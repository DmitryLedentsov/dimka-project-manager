from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, TextIO

from .config import Settings
from .utils import is_relative_to


class ComposeError(RuntimeError):
    pass


def _json_items(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    if not text:
        return []
    try:
        value = json.loads(text)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
    except json.JSONDecodeError:
        pass
    result: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def _labels(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    if isinstance(value, list):
        result: dict[str, str] = {}
        for item in value:
            key, _, content = str(item).partition("=")
            result[key] = content
        return result
    return {}


class ComposeRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _repository(self, project: dict[str, Any]) -> Path:
        return Path(project["repo_path"]).resolve()

    def compose_path(self, project: dict[str, Any]) -> Path:
        repository = self._repository(project)
        configured = Path(str(project.get("compose_file") or "compose.yml"))
        path = configured if configured.is_absolute() else repository / configured
        path = path.resolve()
        if not is_relative_to(path, repository):
            raise ComposeError("Compose file must stay inside the repository")
        if not path.is_file():
            raise ComposeError(f"Compose file does not exist: {path}")
        return path

    def env_path(self, project: dict[str, Any]) -> Path | None:
        repository = self._repository(project)
        configured = str(project.get("env_file") or "").strip()
        if configured:
            path = Path(configured)
            path = path if path.is_absolute() else (repository / path)
            path = path.resolve()
            if not path.is_file():
                raise ComposeError(f"Compose environment file does not exist: {path}")
            return path
        candidates = (
            Path(f"/etc/dpm/projects/{project['name']}.env"),
            Path(f"/etc/{project['name']}/compose.env"),
            repository / ".env",
        )
        return next((path.resolve() for path in candidates if path.is_file()), None)

    def command(self, project: dict[str, Any], *args: str) -> list[str]:
        command = [
            "docker",
            "compose",
            "--project-name",
            str(project["compose_project_name"]),
            "--file",
            str(self.compose_path(project)),
        ]
        env_path = self.env_path(project)
        if env_path:
            command.extend(["--env-file", str(env_path)])
        command.extend(args)
        return command

    @staticmethod
    def _run(
        command: list[str],
        *,
        cwd: Path,
        timeout: int = 120,
        log: TextIO | None = None,
    ) -> str:
        environment = os.environ.copy()
        environment.update({"COMPOSE_ANSI": "never", "DOCKER_BUILDKIT": "1"})
        if log is not None:
            log.write(f"$ {' '.join(command)}\n")
            log.flush()
            try:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
                return_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                try:
                    os.killpg(process.pid, 9)
                except ProcessLookupError:
                    pass
                raise ComposeError(f"Command timed out after {timeout}s: {' '.join(command)}") from exc
            except OSError as exc:
                raise ComposeError(str(exc)) from exc
            if return_code != 0:
                raise ComposeError(f"Command exited with {return_code}: {' '.join(command)}")
            return ""
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ComposeError(str(exc)) from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "Docker Compose command failed"
            raise ComposeError(detail)
        return result.stdout.strip()

    def config(self, project: dict[str, Any]) -> dict[str, Any]:
        output = self._run(
            self.command(project, "config", "--format", "json"),
            cwd=self._repository(project),
            timeout=120,
        )
        try:
            config = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ComposeError("Docker Compose returned invalid config JSON") from exc
        if not isinstance(config, dict) or not isinstance(config.get("services"), dict):
            raise ComposeError("Compose configuration does not define services")
        return config

    def validate(self, project: dict[str, Any]) -> dict[str, Any]:
        config = self.config(project)
        errors: list[str] = []
        for name, service in config["services"].items():
            restart = str(service.get("restart") or "no").lower()
            if restart not in {"", "no", "none"}:
                errors.append(f"service {name}: restart policy '{restart}' is not allowed")
            deploy_policy = (service.get("deploy") or {}).get("restart_policy")
            if deploy_policy:
                errors.append(f"service {name}: deploy.restart_policy is not allowed")
        if errors:
            raise ComposeError("Automatic restarts are disabled by DPM: " + "; ".join(errors))
        return config

    def _ps(self, project: dict[str, Any]) -> list[dict[str, Any]]:
        output = self._run(
            self.command(project, "ps", "--all", "--format", "json"),
            cwd=self._repository(project),
            timeout=60,
        )
        return _json_items(output)

    def _stats(self, container_ids: list[str]) -> dict[str, dict[str, str]]:
        if not container_ids:
            return {}
        output = self._run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", *container_ids],
            cwd=Path("/"),
            timeout=30,
        )
        result: dict[str, dict[str, str]] = {}
        for item in _json_items(output):
            key = str(item.get("ID") or item.get("Container") or "")
            if key:
                result[key] = {str(k): str(v) for k, v in item.items()}
        return result

    @staticmethod
    def _service_status(state: str, health: str, exit_code: int | None, job: bool) -> str:
        state = state.lower()
        health = health.lower()
        if state == "running":
            if health == "healthy" or not health:
                return "healthy" if health else "running"
            if health == "starting":
                return "starting"
            if health == "unhealthy":
                return "unhealthy"
            return "running"
        if state in {"created", "restarting"}:
            return state
        if state in {"exited", "dead"}:
            if job and (exit_code or 0) == 0:
                return "completed"
            return "failed" if (exit_code or 0) != 0 else "stopped"
        if state in {"paused", "removing"}:
            return state
        return "stopped"

    def services(self, project: dict[str, Any], *, include_stats: bool = True) -> list[dict[str, Any]]:
        try:
            config = self.config(project)
        except ComposeError as exc:
            return [{
                "name": "compose",
                "status": "invalid",
                "state": "",
                "health": "",
                "image": "—",
                "container_id": None,
                "container_name": None,
                "exit_code": None,
                "error": str(exc),
                "depends_on": [],
                "ports": [],
                "labels": {},
                "role": "compose",
                "job": False,
                "stats": {},
            }]
        ps_by_service = {str(item.get("Service") or item.get("service") or ""): item for item in self._ps(project)}
        container_ids = [
            str(item.get("ID"))
            for item in ps_by_service.values()
            if item.get("ID")
        ]
        stats = self._stats(container_ids) if include_stats else {}
        services: list[dict[str, Any]] = []
        for name, spec in config["services"].items():
            labels = _labels(spec.get("labels"))
            job = labels.get("dpm.job", "false").lower() == "true"
            item = ps_by_service.get(name, {})
            state = str(item.get("State") or item.get("state") or "")
            health = str(item.get("Health") or item.get("health") or "")
            raw_exit = item.get("ExitCode", item.get("exit_code"))
            try:
                exit_code = int(raw_exit) if raw_exit not in {None, ""} else None
            except (TypeError, ValueError):
                exit_code = None
            container_id = str(item.get("ID") or "") or None
            stat = {}
            if container_id:
                stat = stats.get(container_id) or stats.get(container_id[:12]) or {}
            depends = spec.get("depends_on") or {}
            if isinstance(depends, dict):
                depends_on = sorted(str(key) for key in depends)
            elif isinstance(depends, list):
                depends_on = [str(value) for value in depends]
            else:
                depends_on = []
            image = str(item.get("Image") or spec.get("image") or "build")
            services.append({
                "name": name,
                "status": self._service_status(state, health, exit_code, job),
                "state": state or "not created",
                "health": health,
                "image": image,
                "container_id": container_id,
                "container_name": item.get("Name") or item.get("Names"),
                "exit_code": exit_code,
                "error": None,
                "depends_on": depends_on,
                "ports": item.get("Publishers") or spec.get("ports") or [],
                "labels": labels,
                "role": labels.get("dpm.role", "service"),
                "url": labels.get("dpm.url"),
                "job": job,
                "stats": stat,
            })
        return services

    def wait_ready(self, project: dict[str, Any], log: TextIO) -> None:
        deadline = time.monotonic() + self.settings.compose_timeout
        while time.monotonic() < deadline:
            services = self.services(project, include_stats=False)
            states = {item["name"]: item["status"] for item in services}
            log.write("compose state: " + ", ".join(f"{name}={state}" for name, state in states.items()) + "\n")
            log.flush()
            failures = [item for item in services if item["status"] in {"failed", "unhealthy", "invalid"}]
            if failures:
                detail = ", ".join(f"{item['name']}={item['status']}" for item in failures)
                raise ComposeError(f"Compose service failed: {detail}")
            pending = [item for item in services if item["status"] not in {"running", "healthy", "completed"}]
            if not pending:
                return
            time.sleep(2)
        raise ComposeError(f"Compose project did not become ready within {self.settings.compose_timeout}s")

    def build(self, project: dict[str, Any], log: TextIO, *, pull: bool = True) -> None:
        self.validate(project)
        if pull:
            self._run(
                self.command(project, "pull", "--ignore-buildable"),
                cwd=self._repository(project),
                timeout=900,
                log=log,
            )
        self._run(
            self.command(project, "build", "--pull"),
            cwd=self._repository(project),
            timeout=1800,
            log=log,
        )

    def apply(self, project: dict[str, Any], log: TextIO) -> None:
        self._run(
            self.command(project, "up", "--detach", "--remove-orphans"),
            cwd=self._repository(project),
            timeout=600,
            log=log,
        )
        self.wait_ready(project, log)

    def deploy(self, project: dict[str, Any], log: TextIO, *, pull: bool = True) -> None:
        self.build(project, log, pull=pull)
        self.apply(project, log)

    def start_project(self, project: dict[str, Any]) -> None:
        self.validate(project)
        self._run(
            self.command(project, "up", "--detach"),
            cwd=self._repository(project),
            timeout=600,
        )
        with open(os.devnull, "w", encoding="utf-8") as sink:
            self.wait_ready(project, sink)

    def stop_project(self, project: dict[str, Any]) -> None:
        self._run(self.command(project, "stop"), cwd=self._repository(project), timeout=300)

    def remove_project(self, project: dict[str, Any]) -> None:
        try:
            self._run(
                self.command(project, "down", "--remove-orphans"),
                cwd=self._repository(project),
                timeout=600,
            )
        except ComposeError:
            pass

    def start_service(self, project: dict[str, Any], name: str) -> None:
        self.validate(project)
        self._run(
            self.command(project, "up", "--detach", "--no-deps", name),
            cwd=self._repository(project),
            timeout=600,
        )

    def stop_service(self, project: dict[str, Any], name: str) -> None:
        self._run(self.command(project, "stop", name), cwd=self._repository(project), timeout=300)

    def restart_service(self, project: dict[str, Any], name: str) -> None:
        self._run(self.command(project, "restart", name), cwd=self._repository(project), timeout=300)

    def service_logs(self, project: dict[str, Any], name: str, lines: int) -> str:
        return self._run(
            self.command(project, "logs", "--no-color", "--timestamps", "--tail", str(lines), name),
            cwd=self._repository(project),
            timeout=60,
        )

    @staticmethod
    def safe_service_name(name: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
            raise ComposeError("Invalid Compose service name")
        return name
