from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import psutil

from .db import Database
from .utils import is_relative_to, utc_now


class ServiceError(RuntimeError):
    pass


class SupervisorBase:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._processes: dict[int, subprocess.Popen[Any]] = {}
        self._locks: defaultdict[int, threading.RLock] = defaultdict(threading.RLock)
        self._stop_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None

    def start_monitoring(self) -> None:
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self.reconcile()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="dpm-process-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=3)

    def reconcile(self) -> None:
        services = self.db.fetchall(
            "SELECT * FROM services WHERE component_type = 'process'"
        )
        for service in services:
            pid = service.get("pid")
            status = str(service.get("status") or "unknown")
            if pid and self._pid_alive(int(pid)):
                self.db.update(
                    "services",
                    service["id"],
                    {"status": "running", "updated_at": utc_now()},
                )
                continue

            if status in {"failed", "unhealthy"}:
                next_status = status
            elif status in {"running", "starting", "restarting"}:
                next_status = "failed"
            else:
                next_status = "stopped"

            values: dict[str, Any] = {
                "pid": None,
                "status": next_status,
                "updated_at": utc_now(),
            }
            if next_status == "failed" and status != "failed":
                values["last_error"] = "Process was not running when DPM started"
                values["stopped_at"] = utc_now()
            self.db.update("services", service["id"], values)

        # DPM never starts a component solely because its own daemon restarted.
        # Healthy child processes survive the manager restart via KillMode=process;
        # stopped and failed components wait for an explicit project/component action.

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            process = psutil.Process(pid)
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def get_service(self, service_id: int) -> dict[str, Any]:
        service = self.db.fetchone(
            """
            SELECT s.*, p.name AS project_name, p.repo_path, p.repository_url,
                   p.branch, p.deployed_commit, p.remote_commit, p.commit_message,
                   p.commit_time, p.deploy_status, p.deploy_stage,
                   p.last_error AS project_error, p.desired_state AS project_desired_state
              FROM services s
              JOIN projects p ON p.id = s.project_id
             WHERE s.id = ? AND s.component_type = 'process'
            """,
            [service_id],
        )
        if not service:
            raise ServiceError("Process component not found")
        return service

    def list_services(self) -> list[dict[str, Any]]:
        services = self.db.fetchall(
            """
            SELECT s.*, p.name AS project_name, p.repository_url, p.branch,
                   p.deployed_commit, p.remote_commit, p.deploy_status,
                   p.deploy_stage, p.last_error AS project_error
              FROM services s
              JOIN projects p ON p.id = s.project_id
             WHERE s.component_type = 'process'
             ORDER BY p.name, s.name
            """
        )
        return [self.enrich_service(service) for service in services]

    def enrich_service(self, service: dict[str, Any]) -> dict[str, Any]:
        service = dict(service)
        pid = service.get("pid")
        alive = bool(pid and self._pid_alive(int(pid)))
        if service.get("status") in {"running", "starting", "unhealthy"} and not alive:
            service["status"] = "failed"
        service["component_type"] = "process"
        service["alive"] = alive
        service["command"] = Database.decode_json(service.get("command_json"), [])
        service["environment"] = Database.decode_json(service.get("environment_json"), {})
        service["healthcheck"] = Database.decode_json(service.get("healthcheck_json"), None)
        service["depends_on"] = Database.decode_json(service.get("depends_on_json"), [])
        service["config"] = Database.decode_json(service.get("config_json"), {})
        service["runtime"] = Database.decode_json(service.get("runtime_json"), {})
        service["cpu_percent"] = 0.0
        service["memory_mb"] = 0.0
        service["uptime_seconds"] = None
        if alive and pid:
            try:
                process = psutil.Process(int(pid))
                service["cpu_percent"] = round(process.cpu_percent(interval=0.0), 1)
                service["memory_mb"] = round(process.memory_info().rss / 1024 / 1024, 1)
                service["uptime_seconds"] = max(0, int(time.time() - process.create_time()))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return service

    def _load_environment(self, service: dict[str, Any], cwd: Path) -> dict[str, str]:
        environment = os.environ.copy()
        configured = Database.decode_json(service.get("environment_json"), {})
        environment.update({str(key): str(value) for key, value in configured.items()})
        environment_file = service.get("environment_file")
        if environment_file:
            path = Path(environment_file)
            if not path.is_absolute():
                path = cwd / path
            if not path.exists():
                raise ServiceError(f"Environment file does not exist: {path}")
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                environment[key.strip()] = value.strip().strip("'\"")
        environment["DPM_COMPONENT_ID"] = str(service["id"])
        environment["DPM_SERVICE_ID"] = str(service["id"])
        environment["DPM_PROJECT"] = str(service.get("project_name", ""))
        return environment

    def _command(self, service: dict[str, Any]) -> list[str]:
        command = Database.decode_json(service.get("command_json"), [])
        if isinstance(command, str):
            return ["/bin/bash", "-lc", command]
        if isinstance(command, list) and command:
            return [str(part) for part in command]
        raise ServiceError("Process command is empty")

    def _service_cwd(self, service: dict[str, Any]) -> Path:
        root = Path(service["repo_path"]).resolve()
        configured = Path(service.get("working_directory") or ".")
        cwd = configured if configured.is_absolute() else (root / configured)
        cwd = cwd.resolve()
        if not is_relative_to(cwd, root):
            raise ServiceError("Working directory must stay inside the repository")
        if not cwd.exists():
            raise ServiceError(f"Working directory does not exist: {cwd}")
        return cwd
