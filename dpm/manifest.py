from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ManifestError(ValueError):
    pass


def _duration_seconds(value: Any, default: int) -> int:
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


def _command(value: Any, field_name: str) -> list[str] | str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return [str(item) for item in value]
    raise ManifestError(f"{field_name} must be a non-empty string or string list")


def _depends_on(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ManifestError(f"{field_name} must be a string list")
    return [str(item).strip() for item in value if str(item).strip()]


def _process_healthcheck(value: Any, field_name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ManifestError(f"{field_name} must be an object")

    timeout = _duration_seconds(value.get("timeout", value.get("timeout_seconds")), 30)
    interval = _duration_seconds(value.get("interval", value.get("interval_seconds")), 1)
    if value.get("tcp"):
        endpoint = str(value["tcp"]).strip()
        match = re.fullmatch(r"([^:\s]+):(\d{1,5})", endpoint)
        if not match:
            raise ManifestError(f"{field_name}.tcp must be HOST:PORT")
        host, port = match.groups()
        return {
            "type": "command",
            "command": f"exec 3<>/dev/tcp/{host}/{port}",
            "timeout_seconds": timeout,
            "interval_seconds": interval,
        }
    if value.get("http"):
        return {
            "type": "http",
            "url": str(value["http"]),
            "timeout_seconds": timeout,
            "interval_seconds": interval,
        }
    if value.get("command"):
        return {
            "type": "command",
            "command": value["command"],
            "timeout_seconds": timeout,
            "interval_seconds": interval,
        }
    # Backward-compatible first-generation healthcheck shape.
    if value.get("type") in {"http", "command"}:
        result = dict(value)
        result["timeout_seconds"] = timeout
        result["interval_seconds"] = interval
        return result
    raise ManifestError(f"{field_name} must define tcp, http or command")


@dataclass(frozen=True)
class ComponentDefinition:
    name: str
    type_name: str
    config: dict[str, Any]
    depends_on: list[str] = field(default_factory=list)
    enabled: bool = True

    @property
    def initial_status(self) -> str:
        return "unpublished" if self.type_name == "static" else "stopped"

    @property
    def command(self) -> list[str] | str:
        return self.config.get("command", [])

    @property
    def cwd(self) -> str:
        return str(self.config.get("cwd", "."))

    @property
    def env(self) -> dict[str, str]:
        value = self.config.get("env") or {}
        return {str(key): str(item) for key, item in value.items()}

    @property
    def env_file(self) -> str | None:
        value = self.config.get("env_file")
        return str(value) if value else None

    @property
    def process_healthcheck(self) -> dict[str, Any] | None:
        return self.config.get("_process_healthcheck")


@dataclass(frozen=True)
class ProjectManifest:
    name: str | None
    build_commands: list[str]
    components: list[ComponentDefinition]

    @property
    def services(self) -> list[ComponentDefinition]:
        """Compatibility alias for deployment code from DPM 0.1."""
        return self.components


def _parse_process(name: str, raw: dict[str, Any], path: str) -> ComponentDefinition:
    environment = raw.get("env", raw.get("environment", {})) or {}
    if not isinstance(environment, dict):
        raise ManifestError(f"{path}.env must be an object")
    config = {
        "command": _command(raw.get("command"), f"{path}.command"),
        "cwd": str(raw.get("cwd", raw.get("working_directory", "."))),
        "env": {str(key): str(value) for key, value in environment.items()},
        "env_file": (
            str(raw.get("env_file", raw.get("environment_file")))
            if raw.get("env_file", raw.get("environment_file"))
            else None
        ),
        "healthcheck": raw.get("healthcheck"),
        "_process_healthcheck": _process_healthcheck(
            raw.get("healthcheck"), f"{path}.healthcheck"
        ),
    }
    return ComponentDefinition(
        name=name,
        type_name="process",
        config=config,
        depends_on=_depends_on(raw.get("depends_on"), f"{path}.depends_on"),
        enabled=bool(raw.get("enabled", True)),
    )


def _parse_static(name: str, raw: dict[str, Any], path: str) -> ComponentDefinition:
    source = str(raw.get("source") or "").strip()
    target = str(raw.get("target") or "").strip()
    if not source:
        raise ManifestError(f"{path}.source is required")
    if not target:
        raise ManifestError(f"{path}.target is required")
    healthcheck = raw.get("healthcheck")
    if healthcheck is not None and not isinstance(healthcheck, dict):
        raise ManifestError(f"{path}.healthcheck must be an object")
    config = {
        "source": source,
        "target": target,
        "url": str(raw.get("url")) if raw.get("url") else None,
        "index": str(raw.get("index", "index.html")),
        "healthcheck": healthcheck or {},
    }
    return ComponentDefinition(
        name=name,
        type_name="static",
        config=config,
        depends_on=_depends_on(raw.get("depends_on"), f"{path}.depends_on"),
        enabled=bool(raw.get("enabled", True)),
    )


def _parse_component(name: str, raw: Any, path: str) -> ComponentDefinition:
    if not isinstance(raw, dict):
        raise ManifestError(f"{path} must be an object")
    type_name = str(raw.get("type", "process")).strip().lower()
    if type_name == "process":
        return _parse_process(name, raw, path)
    if type_name == "static":
        return _parse_static(name, raw, path)
    raise ManifestError(f"Unsupported component type for {name}: {type_name}")


def _legacy_components(data: dict[str, Any]) -> dict[str, Any] | None:
    raw_services = data.get("services")
    if raw_services is None:
        return None
    if not isinstance(raw_services, list) or not raw_services:
        raise ManifestError("services must be a non-empty list")
    result: dict[str, Any] = {}
    for index, raw in enumerate(raw_services):
        if not isinstance(raw, dict):
            raise ManifestError(f"services[{index}] must be an object")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ManifestError(f"services[{index}].name is required")
        if name in result:
            raise ManifestError(f"Duplicate component name: {name}")
        converted = dict(raw)
        converted["type"] = "process"
        result[name] = converted
    return result


def load_manifest(repository: Path) -> ProjectManifest:
    path = repository / "dpm.yaml"
    if not path.exists():
        raise ManifestError("Repository does not contain dpm.yaml")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ManifestError(f"Invalid dpm.yaml: {exc}") from exc

    if data.get("version") != 1:
        raise ManifestError("dpm.yaml must contain 'version: 1'")

    project_data = data.get("project") or {}
    build_data = data.get("build") or []
    if isinstance(build_data, dict):
        build_data = build_data.get("commands") or []
    if not isinstance(build_data, list) or not all(
        isinstance(command, str) and command.strip() for command in build_data
    ):
        raise ManifestError("build must be a list of shell commands")

    raw_components = data.get("components")
    if raw_components is None:
        raw_components = _legacy_components(data)
    if not isinstance(raw_components, dict) or not raw_components:
        raise ManifestError("dpm.yaml must define a non-empty components map")

    components: list[ComponentDefinition] = []
    names: set[str] = set()
    for raw_name, raw in raw_components.items():
        name = str(raw_name).strip()
        if not name:
            raise ManifestError("Component name cannot be empty")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name):
            raise ManifestError(f"Unsupported component name: {name}")
        if name in names:
            raise ManifestError(f"Duplicate component name: {name}")
        names.add(name)
        components.append(_parse_component(name, raw, f"components.{name}"))

    unknown_dependencies = {
        dependency
        for component in components
        for dependency in component.depends_on
        if dependency not in names
    }
    if unknown_dependencies:
        raise ManifestError(
            "Unknown component dependencies: " + ", ".join(sorted(unknown_dependencies))
        )

    graph = {component.name: set(component.depends_on) for component in components}
    remaining = set(graph)
    resolved: set[str] = set()
    while remaining:
        ready = {name for name in remaining if graph[name] <= resolved}
        if not ready:
            raise ManifestError("Component dependency graph contains a cycle")
        resolved.update(ready)
        remaining -= ready

    return ProjectManifest(
        name=str(project_data.get("name")).strip() if project_data.get("name") else None,
        build_commands=[str(command).strip() for command in build_data],
        components=components,
    )


def display_command(command: list[str] | str) -> str:
    if isinstance(command, str):
        return command
    return shlex.join(command)
