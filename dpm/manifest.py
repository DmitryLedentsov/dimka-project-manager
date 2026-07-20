from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ServiceDefinition:
    name: str
    command: list[str] | str
    working_directory: str = "."
    environment: dict[str, str] = field(default_factory=dict)
    environment_file: str | None = None
    restart_policy: str = "never"
    healthcheck: dict[str, Any] | None = None
    depends_on: list[str] = field(default_factory=list)
    enabled: bool = True

    def command_for_storage(self) -> list[str] | str:
        return self.command


@dataclass(frozen=True)
class ProjectManifest:
    name: str | None
    build_commands: list[str]
    services: list[ServiceDefinition]


def _command(value: Any, field_name: str) -> list[str] | str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return value
    raise ManifestError(f"{field_name} must be a non-empty string or string list")


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
    build_data = data.get("build") or {}
    build_commands = build_data.get("commands") or []
    if not isinstance(build_commands, list) or not all(
        isinstance(command, str) and command.strip() for command in build_commands
    ):
        raise ManifestError("build.commands must be a list of shell commands")

    raw_services = data.get("services")
    if not isinstance(raw_services, list) or not raw_services:
        raise ManifestError("dpm.yaml must define at least one service")

    services: list[ServiceDefinition] = []
    names: set[str] = set()
    for index, raw in enumerate(raw_services):
        if not isinstance(raw, dict):
            raise ManifestError(f"services[{index}] must be an object")
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ManifestError(f"services[{index}].name is required")
        if name in names:
            raise ManifestError(f"Duplicate service name: {name}")
        names.add(name)

        # Older manifests may still contain always/on-failure. Keep accepting the
        # field so existing repositories deploy, but DPM intentionally stores and
        # enforces 'never': a crashed process must remain visible as FAILED.
        restart = raw.get("restart", "never")
        if isinstance(restart, dict):
            restart = restart.get("policy", "never")
        restart = str(restart).lower()
        if restart not in {"always", "on-failure", "never"}:
            raise ManifestError(f"Invalid restart policy for {name}: {restart}")

        environment = raw.get("environment") or {}
        if not isinstance(environment, dict):
            raise ManifestError(f"services[{index}].environment must be an object")

        depends_on = raw.get("depends_on") or []
        if not isinstance(depends_on, list) or not all(isinstance(item, str) for item in depends_on):
            raise ManifestError(f"services[{index}].depends_on must be a list")

        services.append(
            ServiceDefinition(
                name=name,
                command=_command(raw.get("command"), f"services[{index}].command"),
                working_directory=str(raw.get("working_directory", ".")),
                environment={str(k): str(v) for k, v in environment.items()},
                environment_file=(str(raw["environment_file"]) if raw.get("environment_file") else None),
                restart_policy="never",
                healthcheck=raw.get("healthcheck"),
                depends_on=depends_on,
                enabled=bool(raw.get("enabled", True)),
            )
        )

    unknown_dependencies = {
        dependency
        for service in services
        for dependency in service.depends_on
        if dependency not in names
    }
    if unknown_dependencies:
        raise ManifestError(
            "Unknown service dependencies: " + ", ".join(sorted(unknown_dependencies))
        )

    graph = {service.name: set(service.depends_on) for service in services}
    remaining = set(graph)
    resolved: set[str] = set()
    while remaining:
        ready = {name for name in remaining if graph[name] <= resolved}
        if not ready:
            raise ManifestError("Service dependency graph contains a cycle")
        resolved.update(ready)
        remaining -= ready

    return ProjectManifest(
        name=str(project_data.get("name")).strip() if project_data.get("name") else None,
        build_commands=[command.strip() for command in build_commands],
        services=services,
    )


def display_command(command: list[str] | str) -> str:
    if isinstance(command, str):
        return command
    return shlex.join(command)
