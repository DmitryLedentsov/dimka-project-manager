from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .db import Database
from .supervisor_base import ServiceError
from .utils import utc_now


class LifecycleMixin:
    def start_service(self, service_id: int, *, automatic: bool = False) -> dict[str, Any]:
        with self._locks[service_id]:
            service = self.get_service(service_id)
            pid = service.get("pid")
            if pid and self._pid_alive(int(pid)):
                return self.enrich_service(service)
            if not service.get("enabled") and automatic:
                return self.enrich_service(service)

            cwd = self._service_cwd(service)
            command = self._command(service)
            environment = self._load_environment(service, cwd)
            log_path = Path(service["log_path"])
            log_path.parent.mkdir(parents=True, exist_ok=True)
            now = utc_now()
            self.db.update(
                "services",
                service_id,
                {
                    "status": "starting",
                    "last_error": None,
                    "exit_code": None,
                    "updated_at": now,
                },
            )

            try:
                with log_path.open("a", encoding="utf-8", buffering=1) as log_file:
                    log_file.write(f"\n[{now}] [dpm] starting: {shlex.join(command)}\n")
                    process = subprocess.Popen(
                        command,
                        cwd=cwd,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        close_fds=True,
                    )
            except OSError as exc:
                self.db.update(
                    "services",
                    service_id,
                    {
                        "status": "failed",
                        "last_error": str(exc),
                        "updated_at": utc_now(),
                    },
                )
                raise ServiceError(str(exc)) from exc

            self._processes[service_id] = process
            self.db.update(
                "services",
                service_id,
                {
                    "pid": process.pid,
                    "started_at": utc_now(),
                    "stopped_at": None,
                    "status": "starting",
                    "updated_at": utc_now(),
                },
            )

            time.sleep(0.7)
            exit_code = process.poll()
            if exit_code is not None:
                message = f"Process exited during startup with code {exit_code}"
                self._processes.pop(service_id, None)
                self.db.update(
                    "services",
                    service_id,
                    {
                        "pid": None,
                        "status": "failed",
                        "exit_code": exit_code,
                        "stopped_at": utc_now(),
                        "last_error": message,
                        "updated_at": utc_now(),
                    },
                )
                raise ServiceError(message)

            healthcheck = Database.decode_json(service.get("healthcheck_json"), None)
            if healthcheck:
                healthy, error = self._wait_for_healthcheck(
                    healthcheck,
                    cwd,
                    environment,
                    process=process,
                )
                if not healthy:
                    exit_code = process.poll()
                    failed = exit_code is not None
                    if failed:
                        self._processes.pop(service_id, None)
                    self.db.update(
                        "services",
                        service_id,
                        {
                            "pid": None if failed else process.pid,
                            "status": "failed" if failed else "unhealthy",
                            "exit_code": exit_code,
                            "stopped_at": utc_now() if failed else None,
                            "last_error": error,
                            "updated_at": utc_now(),
                        },
                    )
                    raise ServiceError(error or "Healthcheck failed")

            self.db.update(
                "services",
                service_id,
                {"status": "running", "last_error": None, "updated_at": utc_now()},
            )
            return self.enrich_service(self.get_service(service_id))

    def _wait_for_healthcheck(
        self,
        healthcheck: dict[str, Any],
        cwd: Path,
        environment: dict[str, str],
        *,
        process: subprocess.Popen[Any] | None = None,
    ) -> tuple[bool, str | None]:
        check_type = str(healthcheck.get("type", "http")).lower()
        timeout = max(1, int(healthcheck.get("timeout_seconds", 30)))
        interval = max(0.2, float(healthcheck.get("interval_seconds", 1)))
        deadline = time.monotonic() + timeout
        last_error: str | None = None

        while time.monotonic() < deadline:
            if process is not None:
                exit_code = process.poll()
                if exit_code is not None:
                    return False, f"Process exited during startup with code {exit_code}"
            try:
                if check_type == "http":
                    url = str(healthcheck["url"])
                    request = urllib.request.Request(url, headers={"User-Agent": "DPM/0.1"})
                    with urllib.request.urlopen(request, timeout=3) as response:
                        if 200 <= response.status < 400:
                            return True, None
                        last_error = f"HTTP {response.status}"
                elif check_type == "command":
                    command = healthcheck.get("command")
                    args = ["/bin/bash", "-lc", command] if isinstance(command, str) else command
                    if not args:
                        return False, "Healthcheck command is empty"
                    result = subprocess.run(
                        args,
                        cwd=cwd,
                        env=environment,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                    )
                    if result.returncode == 0:
                        return True, None
                    last_error = f"Healthcheck exited with {result.returncode}"
                else:
                    return False, f"Unknown healthcheck type: {check_type}"
            except (OSError, KeyError, subprocess.TimeoutExpired, urllib.error.URLError) as exc:
                last_error = str(exc)
            time.sleep(interval)
        return False, last_error or "Healthcheck timed out"

    def stop_service(self, service_id: int, *, disable: bool = False) -> dict[str, Any]:
        with self._locks[service_id]:
            service = self.get_service(service_id)
            pid = service.get("pid")
            values: dict[str, Any] = {"status": "stopping", "updated_at": utc_now()}
            if disable:
                values["enabled"] = 0
            self.db.update("services", service_id, values)

            if pid and self._pid_alive(int(pid)):
                try:
                    os.killpg(int(pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline and self._pid_alive(int(pid)):
                    time.sleep(0.2)
                if self._pid_alive(int(pid)):
                    try:
                        os.killpg(int(pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass

            process = self._processes.pop(service_id, None)
            exit_code = process.poll() if process else None
            self.db.update(
                "services",
                service_id,
                {
                    "pid": None,
                    "status": "stopped",
                    "stopped_at": utc_now(),
                    "exit_code": exit_code,
                    "updated_at": utc_now(),
                },
            )
            return self.enrich_service(self.get_service(service_id))

    def restart_service(self, service_id: int) -> dict[str, Any]:
        self.stop_service(service_id)
        self.db.update("services", service_id, {"enabled": 1, "updated_at": utc_now()})
        return self.start_service(service_id)

    def enable_service(self, service_id: int) -> dict[str, Any]:
        self.db.update("services", service_id, {"enabled": 1, "updated_at": utc_now()})
        return self.start_service(service_id)

    def delete_service(self, service_id: int) -> None:
        try:
            self.stop_service(service_id, disable=True)
        except ServiceError:
            pass
        self.db.execute("DELETE FROM services WHERE id = ?", [service_id])

    def _ordered_project_services(self, project_id: int, *, enabled_only: bool) -> list[dict[str, Any]]:
        clause = " AND enabled = 1" if enabled_only else ""
        services = self.db.fetchall(
            f"SELECT * FROM services WHERE project_id = ?{clause}", [project_id]
        )
        by_name = {service["name"]: service for service in services}
        dependencies = {
            name: {
                dep
                for dep in Database.decode_json(service.get("depends_on_json"), [])
                if dep in by_name
            }
            for name, service in by_name.items()
        }
        ordered: list[dict[str, Any]] = []
        resolved: set[str] = set()
        remaining = set(by_name)
        while remaining:
            ready = sorted(name for name in remaining if dependencies[name] <= resolved)
            if not ready:
                raise ServiceError("Service dependency graph contains a cycle")
            for name in ready:
                ordered.append(by_name[name])
                resolved.add(name)
                remaining.remove(name)
        return ordered

    def stop_project(self, project_id: int) -> None:
        services = list(reversed(self._ordered_project_services(project_id, enabled_only=False)))
        for service in services:
            try:
                self.stop_service(service["id"])
            except ServiceError:
                continue

    def start_project(self, project_id: int) -> None:
        services = self._ordered_project_services(project_id, enabled_only=True)
        try:
            for service in services:
                self.start_service(service["id"])
        except ServiceError:
            # Intentionally no rollback: the failed state and logs remain visible.
            raise
