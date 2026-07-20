from __future__ import annotations

import json
import shutil
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .supervisor import ServiceError, ServiceSupervisor
from .utils import tail_file, utc_now


class ComponentError(RuntimeError):
    pass


def _duration_seconds(value: Any, default: int = 30) -> int:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return max(1, int(value))
    text = str(value).strip().lower()
    multipliers = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
    for suffix, multiplier in multipliers.items():
        if text.endswith(suffix):
            try:
                return max(1, int(float(text[: -len(suffix)]) * multiplier))
            except ValueError:
                return default
    try:
        return max(1, int(float(text)))
    except ValueError:
        return default


def _format_bytes(value: int | float | None) -> str:
    size = float(value or 0)
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    if index == 0:
        return f"{int(size)} {units[index]}"
    return f"{size:.1f} {units[index]}"


class ComponentHandler(ABC):
    type_name: str
    type_label: str
    template_name: str
    action_labels: dict[str, str]
    healthy_states: set[str]
    active_states: set[str]
    stopped_states: set[str]

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db

    @abstractmethod
    def enrich(self, component: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def start(self, component_id: int) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def stop(self, component_id: int) -> dict[str, Any]:
        raise NotImplementedError

    def restart(self, component_id: int) -> dict[str, Any]:
        self.stop(component_id)
        return self.start(component_id)

    def logs(self, component: dict[str, Any], lines: int) -> dict[str, Any]:
        return {"logs": tail_file(Path(component["log_path"]), lines), "path": component["log_path"]}

    def delete(self, component_id: int) -> None:
        self.stop(component_id)
        self.db.execute("DELETE FROM services WHERE id = ?", [component_id])

    def prepare_deploy_stop(self, component_id: int) -> None:
        self.stop(component_id)

    def prepare_reconfigure(
        self,
        component: dict[str, Any],
        new_type: str,
        new_config: dict[str, Any],
    ) -> None:
        if new_type != self.type_name:
            self.stop(int(component["id"]))

    def summary(self, component: dict[str, Any]) -> dict[str, Any]:
        return {
            "target_title": component.get("working_directory") or ".",
            "target_subtitle": component.get("deployed_commit") or "not deployed",
            "runtime_title": "—",
            "runtime_subtitle": "inactive",
            "open_url": None,
        }

    def ui(self, component: dict[str, Any]) -> dict[str, Any]:
        status = str(component.get("status") or "unknown")
        active = status in self.active_states
        project_stopped = str(component.get("project_desired_state") or "running") == "stopped"
        return {
            "type_label": self.type_label,
            "actions": dict(self.action_labels),
            "active": active,
            "healthy": status in self.healthy_states,
            "can_start": not active and not project_stopped,
            "can_stop": active,
            "can_restart": active and not project_stopped,
            "project_stopped": project_stopped,
            **self.summary(component),
        }


class ProcessComponentHandler(ComponentHandler):
    type_name = "process"
    type_label = "PROC"
    template_name = "component_process.html"
    action_labels = {"start": "Start", "stop": "Stop", "restart": "Restart"}
    healthy_states = {"running"}
    active_states = {"running", "starting", "unhealthy"}
    stopped_states = {"stopped"}

    def __init__(self, settings: Settings, db: Database, supervisor: ServiceSupervisor) -> None:
        super().__init__(settings, db)
        self.supervisor = supervisor

    def enrich(self, component: dict[str, Any]) -> dict[str, Any]:
        return self.supervisor.enrich_service(component)

    def start(self, component_id: int) -> dict[str, Any]:
        try:
            self.db.update("services", component_id, {"enabled": 1, "updated_at": utc_now()})
            return self.supervisor.start_service(component_id)
        except ServiceError as exc:
            raise ComponentError(str(exc)) from exc

    def stop(self, component_id: int) -> dict[str, Any]:
        try:
            return self.supervisor.stop_service(component_id, disable=False)
        except ServiceError as exc:
            raise ComponentError(str(exc)) from exc

    def restart(self, component_id: int) -> dict[str, Any]:
        try:
            return self.supervisor.restart_service(component_id)
        except ServiceError as exc:
            raise ComponentError(str(exc)) from exc

    def delete(self, component_id: int) -> None:
        try:
            self.supervisor.delete_service(component_id)
        except ServiceError as exc:
            raise ComponentError(str(exc)) from exc

    def summary(self, component: dict[str, Any]) -> dict[str, Any]:
        pid = component.get("pid")
        if pid:
            runtime_title = f"PID {pid}"
            runtime_subtitle = f"{component.get('memory_mb', 0)} MB"
        else:
            runtime_title = "—"
            runtime_subtitle = "inactive"
        return {
            "target_title": component.get("working_directory") or ".",
            "target_subtitle": str(component.get("deployed_commit") or "not deployed")[:8],
            "runtime_title": runtime_title,
            "runtime_subtitle": runtime_subtitle,
            "open_url": None,
        }


class StaticComponentHandler(ComponentHandler):
    type_name = "static"
    type_label = "WEB"
    template_name = "component_static.html"
    action_labels = {"start": "Publish", "stop": "Unpublish", "restart": "Republish"}
    healthy_states = {"ready"}
    active_states = {"ready", "publishing"}
    stopped_states = {"unpublished", "stopped"}

    def _component(self, component_id: int) -> dict[str, Any]:
        component = self.db.fetchone(
            """
            SELECT s.*, p.name AS project_name, p.repo_path, p.deployed_commit,
                   p.attempted_commit, p.remote_commit,
                   p.desired_state AS project_desired_state
              FROM services s
              JOIN projects p ON p.id = s.project_id
             WHERE s.id = ? AND s.component_type = 'static'
            """,
            [component_id],
        )
        if not component:
            raise ComponentError("Static component not found")
        return component

    @staticmethod
    def _config(component: dict[str, Any]) -> dict[str, Any]:
        return Database.decode_json(component.get("config_json"), {})

    @staticmethod
    def _runtime(component: dict[str, Any]) -> dict[str, Any]:
        return Database.decode_json(component.get("runtime_json"), {})

    def enrich(self, component: dict[str, Any]) -> dict[str, Any]:
        item = dict(component)
        config = self._config(item)
        runtime = self._runtime(item)
        item.update(
            {
                "component_type": "static",
                "config": config,
                "runtime": runtime,
                "source": config.get("source"),
                "target": config.get("target"),
                "url": config.get("url"),
                "published_commit": runtime.get("published_commit"),
                "published_at": runtime.get("published_at"),
                "file_count": runtime.get("file_count", 0),
                "size_bytes": runtime.get("size_bytes", 0),
                "alive": item.get("status") == "ready",
                "pid": None,
                "cpu_percent": 0.0,
                "memory_mb": 0.0,
                "uptime_seconds": None,
                "depends_on": Database.decode_json(item.get("depends_on_json"), []),
            }
        )
        return item

    @staticmethod
    def _write_log(path: Path, message: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{utc_now()}] [static] {message}\n")

    @staticmethod
    def _marker(component: dict[str, Any]) -> str:
        return f"{component['project_id']}:{component['id']}"

    def _owner_path(self, target: Path, component: dict[str, Any]) -> Path:
        return target.parent / f".{target.name}.dpm-owner-{component['id']}"

    def _owned_target(self, target: Path, component: dict[str, Any]) -> bool:
        marker_path = self._owner_path(target, component)
        try:
            return marker_path.is_file() and marker_path.read_text(encoding="utf-8").strip() == self._marker(component)
        except OSError:
            return False

    def _check_http(self, config: dict[str, Any]) -> tuple[bool, str | None, int | None]:
        healthcheck = config.get("healthcheck") or {}
        url = healthcheck.get("http") or config.get("url")
        if not url:
            return True, None, None
        timeout = _duration_seconds(healthcheck.get("timeout"), 15)
        deadline = time.monotonic() + timeout
        last_error: str | None = None
        while time.monotonic() < deadline:
            started = time.monotonic()
            try:
                request = urllib.request.Request(str(url), headers={"User-Agent": "DPM/0.2"})
                with urllib.request.urlopen(request, timeout=min(4, timeout)) as response:
                    latency = int((time.monotonic() - started) * 1000)
                    if 200 <= response.status < 400:
                        return True, None, latency
                    last_error = f"HTTP {response.status}"
            except (OSError, urllib.error.URLError) as exc:
                last_error = str(exc)
            time.sleep(0.5)
        return False, last_error or "HTTP healthcheck timed out", None

    def start(self, component_id: int) -> dict[str, Any]:
        component = self._component(component_id)
        config = self._config(component)
        repository = Path(component["repo_path"]).resolve()
        source_value = str(config.get("source") or "").strip()
        target_value = str(config.get("target") or "").strip()
        if not source_value or not target_value:
            raise ComponentError("Static component requires source and target")
        source = (repository / source_value).resolve()
        try:
            source.relative_to(repository)
        except ValueError as exc:
            raise ComponentError("Static source must stay inside the repository") from exc
        target = Path(target_value).resolve()
        index_file = str(config.get("index") or "index.html")
        if not source.is_dir():
            raise ComponentError(f"Static source directory does not exist: {source}")
        if not (source / index_file).is_file():
            raise ComponentError(f"Static index file does not exist: {source / index_file}")

        log_path = Path(component["log_path"])
        self.db.update("services", component_id, {"status": "publishing", "last_error": None, "updated_at": utc_now()})
        self._write_log(log_path, f"publishing {source} -> {target}")
        staging = target.parent / f".{target.name}.dpm-new-{component_id}"
        backup = target.parent / f".{target.name}.dpm-old-{component_id}"
        owner_path = self._owner_path(target, component)
        had_previous = target.exists()
        activated = False
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(backup, ignore_errors=True)
            shutil.copytree(source, staging)
            if had_previous:
                target.rename(backup)
            staging.rename(target)
            activated = True

            file_count = 0
            size_bytes = 0
            for path in target.rglob("*"):
                if path.is_file():
                    file_count += 1
                    size_bytes += path.stat().st_size
            healthy, error, latency_ms = self._check_http(config)
            if not healthy:
                raise ComponentError(error or "Static healthcheck failed")

            owner_path.write_text(self._marker(component), encoding="utf-8")
            shutil.rmtree(backup, ignore_errors=True)
            release_commit = component.get("attempted_commit") or component.get("remote_commit") or component.get("deployed_commit")
            runtime = {"published_commit": release_commit, "published_at": utc_now(), "file_count": file_count, "size_bytes": size_bytes, "latency_ms": latency_ms}
            self.db.update("services", component_id, {"status": "ready", "runtime_json": json.dumps(runtime), "started_at": utc_now(), "stopped_at": None, "last_error": None, "updated_at": utc_now()})
            self._write_log(log_path, f"published {file_count} files ({size_bytes} bytes)")
            return self.enrich(self._component(component_id))
        except (OSError, ComponentError) as exc:
            shutil.rmtree(staging, ignore_errors=True)
            if activated and target.exists():
                shutil.rmtree(target, ignore_errors=True)
            restored = False
            if backup.exists():
                backup.rename(target)
                restored = True
                self._write_log(log_path, "restored previous static release")
            elif not had_previous:
                owner_path.unlink(missing_ok=True)
            self.db.update("services", component_id, {"status": "ready" if restored else "failed", "last_error": str(exc), "updated_at": utc_now()})
            self._write_log(log_path, f"publication failed: {exc}")
            if isinstance(exc, ComponentError):
                raise
            raise ComponentError(str(exc)) from exc

    def stop(self, component_id: int) -> dict[str, Any]:
        component = self._component(component_id)
        config = self._config(component)
        target_value = str(config.get("target") or "").strip()
        if not target_value:
            raise ComponentError("Static component target is empty")
        target = Path(target_value).resolve()
        owner_path = self._owner_path(target, component)
        log_path = Path(component["log_path"])
        try:
            if target.exists():
                if not self._owned_target(target, component):
                    raise ComponentError(f"Refusing to remove unowned static target: {target}")
                shutil.rmtree(target)
            owner_path.unlink(missing_ok=True)
            self.db.update("services", component_id, {"status": "unpublished", "stopped_at": utc_now(), "last_error": None, "updated_at": utc_now()})
            self._write_log(log_path, f"unpublished {target}")
            return self.enrich(self._component(component_id))
        except OSError as exc:
            self.db.update("services", component_id, {"status": "failed", "last_error": str(exc), "updated_at": utc_now()})
            raise ComponentError(str(exc)) from exc

    def prepare_deploy_stop(self, component_id: int) -> None:
        # Keep the currently published release online until the new artifact has
        # passed its own HTTP check and is atomically activated.
        component = self._component(component_id)
        self._write_log(Path(component["log_path"]), "keeping current release during project deploy")

    def prepare_reconfigure(self, component: dict[str, Any], new_type: str, new_config: dict[str, Any]) -> None:
        old_target = str(self._config(component).get("target") or "")
        new_target = str(new_config.get("target") or "")
        if new_type != self.type_name or old_target != new_target:
            self.stop(int(component["id"]))

    def summary(self, component: dict[str, Any]) -> dict[str, Any]:
        return {
            "target_title": component.get("target") or "—",
            "target_subtitle": component.get("url") or component.get("source") or "static publication",
            "runtime_title": _format_bytes(component.get("size_bytes")),
            "runtime_subtitle": f"{component.get('file_count', 0)} files",
            "open_url": component.get("url"),
        }


class ComponentManager:
    def __init__(self, settings: Settings, db: Database, process_supervisor: ServiceSupervisor) -> None:
        self.settings = settings
        self.db = db
        self.process_supervisor = process_supervisor
        self.handlers: dict[str, ComponentHandler] = {}
        self.register(ProcessComponentHandler(settings, db, process_supervisor))
        self.register(StaticComponentHandler(settings, db))

    def register(self, handler: ComponentHandler) -> None:
        if handler.type_name in self.handlers:
            raise ComponentError(f"Duplicate component handler: {handler.type_name}")
        self.handlers[handler.type_name] = handler

    def handler_for(self, component: dict[str, Any] | str) -> ComponentHandler:
        type_name = component if isinstance(component, str) else str(component.get("component_type") or "process")
        handler = self.handlers.get(type_name)
        if not handler:
            raise ComponentError(f"Unsupported component type: {type_name}")
        return handler

    def start_monitoring(self) -> None:
        self.process_supervisor.start_monitoring()

    def shutdown(self) -> None:
        self.process_supervisor.shutdown()

    def get_component_raw(self, component_id: int) -> dict[str, Any]:
        component = self.db.fetchone(
            """
            SELECT s.*, p.name AS project_name, p.repo_path, p.repository_url,
                   p.branch, p.deployed_commit, p.remote_commit, p.attempted_commit,
                   p.commit_message, p.commit_time, p.deploy_status, p.deploy_stage,
                   p.last_error AS project_error, p.desired_state AS project_desired_state
              FROM services s
              JOIN projects p ON p.id = s.project_id
             WHERE s.id = ?
            """,
            [component_id],
        )
        if not component:
            raise ComponentError("Component not found")
        return component

    def _enrich(self, component: dict[str, Any]) -> dict[str, Any]:
        handler = self.handler_for(component)
        item = handler.enrich(component)
        item["component_type"] = str(component.get("component_type") or "process")
        item["config"] = Database.decode_json(component.get("config_json"), {})
        item["runtime"] = Database.decode_json(component.get("runtime_json"), {})
        item["depends_on"] = Database.decode_json(component.get("depends_on_json"), [])
        item["ui"] = handler.ui(item)
        item["actions"] = item["ui"]["actions"]
        item["healthy"] = item["ui"]["healthy"]
        return item

    def get_component(self, component_id: int) -> dict[str, Any]:
        return self._enrich(self.get_component_raw(component_id))

    def list_components(self, project_id: int | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT s.*, p.name AS project_name, p.repo_path, p.repository_url,
                   p.branch, p.deployed_commit, p.remote_commit, p.attempted_commit,
                   p.deploy_status, p.deploy_stage, p.last_error AS project_error,
                   p.desired_state AS project_desired_state
              FROM services s
              JOIN projects p ON p.id = s.project_id
        """
        params: list[Any] = []
        if project_id is not None:
            query += " WHERE s.project_id = ?"
            params.append(project_id)
        query += " ORDER BY p.name, s.name"
        return [self._enrich(row) for row in self.db.fetchall(query, params)]

    def _assert_project_running(self, component: dict[str, Any]) -> None:
        if str(component.get("project_desired_state") or "running") != "running":
            raise ComponentError("Project is stopped; start the project first")

    def start_component(self, component_id: int) -> dict[str, Any]:
        component = self.get_component_raw(component_id)
        self._assert_project_running(component)
        return self.handler_for(component).start(component_id)

    def stop_component(self, component_id: int) -> dict[str, Any]:
        component = self.get_component_raw(component_id)
        return self.handler_for(component).stop(component_id)

    def restart_component(self, component_id: int) -> dict[str, Any]:
        component = self.get_component_raw(component_id)
        self._assert_project_running(component)
        return self.handler_for(component).restart(component_id)

    def delete_component(self, component_id: int) -> None:
        component = self.get_component_raw(component_id)
        self.handler_for(component).delete(component_id)

    def prepare_reconfigure(self, component_id: int, new_type: str, new_config: dict[str, Any]) -> None:
        component = self.get_component_raw(component_id)
        self.handler_for(component).prepare_reconfigure(component, new_type, new_config)

    def component_logs(self, component_id: int, lines: int = 250) -> dict[str, Any]:
        component = self.get_component_raw(component_id)
        return self.handler_for(component).logs(component, lines)

    def component_template(self, component_id: int) -> str:
        component = self.get_component_raw(component_id)
        return self.handler_for(component).template_name

    def _ordered_project_components(self, project_id: int, *, enabled_only: bool) -> list[dict[str, Any]]:
        clause = " AND enabled = 1" if enabled_only else ""
        components = self.db.fetchall(f"SELECT * FROM services WHERE project_id = ?{clause}", [project_id])
        by_name = {component["name"]: component for component in components}
        dependencies = {name: {dependency for dependency in Database.decode_json(component.get("depends_on_json"), []) if dependency in by_name} for name, component in by_name.items()}
        ordered: list[dict[str, Any]] = []
        resolved: set[str] = set()
        remaining = set(by_name)
        while remaining:
            ready = sorted(name for name in remaining if dependencies[name] <= resolved)
            if not ready:
                raise ComponentError("Component dependency graph contains a cycle")
            for name in ready:
                ordered.append(by_name[name])
                resolved.add(name)
                remaining.remove(name)
        return ordered

    def start_project(self, project_id: int) -> None:
        for component in self._ordered_project_components(project_id, enabled_only=True):
            self.start_component(int(component["id"]))

    def stop_project(self, project_id: int) -> None:
        for component in reversed(self._ordered_project_components(project_id, enabled_only=False)):
            try:
                self.stop_component(int(component["id"]))
            except ComponentError:
                continue

    def stop_project_for_deploy(self, project_id: int) -> None:
        for component in reversed(self._ordered_project_components(project_id, enabled_only=False)):
            self.handler_for(component).prepare_deploy_stop(int(component["id"]))

    def project_summary(self, project_id: int) -> dict[str, int]:
        components = self.list_components(project_id)
        return {"total": len(components), "ready": sum(1 for item in components if item.get("healthy")), "failed": sum(1 for item in components if item.get("status") in {"failed", "unhealthy"})}

    def list_services(self) -> list[dict[str, Any]]:
        return self.list_components()

    def get_service(self, component_id: int) -> dict[str, Any]:
        return self.get_component_raw(component_id)

    def enrich_service(self, component: dict[str, Any]) -> dict[str, Any]:
        return self._enrich(component)
