from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import pyzipper

from .compose import ComposeError, ComposeRuntime
from .config import Settings
from .db import Database
from .projects import ProjectError, ProjectManager
from .utils import is_relative_to, utc_now


class MigrationError(RuntimeError):
    pass


_ARCHIVE_FORMAT = "dpm-migration"
_ARCHIVE_VERSION = 1
_MAX_CONFIG_SIZE = 16 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_docker(args: list[str], *, timeout: int = 120) -> str:
    try:
        result = subprocess.run(
            ["docker", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MigrationError(str(exc)) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Docker command failed"
        raise MigrationError(detail)
    return result.stdout.strip()


def _safe_relative(value: str) -> Path:
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise MigrationError(f"Unsafe archive path: {value}")
    return Path(*pure.parts)


class MigrationManager:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        projects: ProjectManager,
        compose: ComposeRuntime,
    ) -> None:
        self.settings = settings
        self.db = db
        self.projects = projects
        self.compose = compose
        self._lock = threading.Lock()

    def _project_definition(self, project: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": str(project["name"]),
            "repository_url": str(project["repository_url"]),
            "branch": str(project.get("branch") or "master"),
            "compose_file": str(project.get("compose_file") or "compose.yml"),
            "compose_project_name": str(project.get("compose_project_name") or project["name"]),
            "env_file": project.get("env_file"),
            "auto_update": bool(project.get("auto_update")),
            "poll_interval": int(project.get("poll_interval") or self.settings.poll_interval),
            "desired_state": str(project.get("desired_state") or "running"),
            "deployed_commit": project.get("deployed_commit"),
        }

    @staticmethod
    def _paths_from_env_file(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        result: list[str] = []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    result.append(item)
                elif isinstance(item, dict) and item.get("path"):
                    result.append(str(item["path"]))
        elif isinstance(value, dict) and value.get("path"):
            result.append(str(value["path"]))
        return result

    def _config_paths(self, project: dict[str, Any], warnings: list[str]) -> list[Path]:
        repository = Path(project["repo_path"]).resolve()
        paths: set[Path] = set()
        stored_env = str(project.get("env_file") or "").strip()
        if stored_env:
            candidate = Path(stored_env)
            paths.add((candidate if candidate.is_absolute() else repository / candidate).resolve())
        try:
            config = self.compose.config(project)
        except ComposeError as exc:
            warnings.append(f"{project['name']}: Compose config could not be inspected: {exc}")
            return sorted(paths)
        for service in (config.get("services") or {}).values():
            if not isinstance(service, dict):
                continue
            for raw in self._paths_from_env_file(service.get("env_file")):
                candidate = Path(raw)
                paths.add((candidate if candidate.is_absolute() else repository / candidate).resolve())
        for section in ("secrets", "configs"):
            for spec in (config.get(section) or {}).values():
                if isinstance(spec, dict) and spec.get("file"):
                    candidate = Path(str(spec["file"]))
                    paths.add((candidate if candidate.is_absolute() else repository / candidate).resolve())
        return sorted(paths)

    def _file_entry(
        self,
        project: dict[str, Any],
        path: Path,
        warnings: list[str],
    ) -> dict[str, Any] | None:
        if not path.is_file():
            warnings.append(f"{project['name']}: config file is missing and was skipped: {path}")
            return None
        size = path.stat().st_size
        if size > _MAX_CONFIG_SIZE:
            warnings.append(f"{project['name']}: config file is larger than 16 MiB and was skipped: {path}")
            return None
        repository = Path(project["repo_path"]).resolve()
        if is_relative_to(path, repository):
            relative = path.relative_to(repository)
            archive_path = f"projects/{project['name']}/files/{relative.as_posix()}"
            location = "repository"
            destination = relative.as_posix()
        else:
            if not is_relative_to(path, Path("/etc")):
                warnings.append(f"{project['name']}: external config outside /etc was skipped: {path}")
                return None
            archive_path = "rootfs/" + str(path).lstrip("/")
            location = "absolute"
            destination = str(path)
        return {
            "project": str(project["name"]),
            "location": location,
            "destination": destination,
            "archive_path": archive_path,
            "mode": stat.S_IMODE(path.stat().st_mode),
            "size": size,
            "sha256": _sha256(path),
            "_source": path,
        }

    @staticmethod
    def _volume_names(compose_project_name: str) -> list[str]:
        output = _run_docker([
            "volume", "ls",
            "--filter", f"label=com.docker.compose.project={compose_project_name}",
            "--format", "{{.Name}}",
        ])
        return [line.strip() for line in output.splitlines() if line.strip()]

    @staticmethod
    def _volume_info(name: str) -> dict[str, Any]:
        raw = _run_docker(["volume", "inspect", name])
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MigrationError(f"Docker returned invalid volume metadata for {name}") from exc
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise MigrationError(f"Docker volume metadata is empty for {name}")
        return items[0]

    def export_archive(
        self,
        destination: Path,
        password: str,
        *,
        include_git_credentials: bool = True,
        include_volumes: bool = False,
    ) -> dict[str, Any]:
        if len(password) < 8:
            raise MigrationError("Archive password must contain at least 8 characters")
        if not self._lock.acquire(blocking=False):
            raise MigrationError("Another migration operation is already running")
        volume_temp_dir: Path | None = None
        stopped_projects: list[dict[str, Any]] = []
        try:
            raw_projects = self.db.fetchall("SELECT * FROM projects ORDER BY name")
            busy = [p["name"] for p in raw_projects if self.projects.is_busy(int(p["id"]))]
            if busy:
                raise MigrationError("Wait for active deployments to finish: " + ", ".join(busy))

            warnings: list[str] = []
            file_entries: list[dict[str, Any]] = []
            seen_files: set[tuple[str, str]] = set()
            for project in raw_projects:
                for path in self._config_paths(project, warnings):
                    entry = self._file_entry(project, path, warnings)
                    if not entry:
                        continue
                    key = (entry["location"], entry["destination"])
                    if key in seen_files:
                        continue
                    seen_files.add(key)
                    file_entries.append(entry)

            ssh_entries: list[dict[str, Any]] = []
            ssh_dir = self.settings.data_dir / ".ssh"
            if include_git_credentials and ssh_dir.is_dir():
                for path in sorted(ssh_dir.rglob("*")):
                    if not path.is_file() or path.is_symlink():
                        continue
                    relative = path.relative_to(ssh_dir)
                    ssh_entries.append({
                        "relative_path": relative.as_posix(),
                        "archive_path": f"ssh/{relative.as_posix()}",
                        "mode": stat.S_IMODE(path.stat().st_mode),
                        "sha256": _sha256(path),
                        "_source": path,
                    })

            volumes: list[dict[str, Any]] = []
            volume_temp_dir = Path(tempfile.mkdtemp(prefix="dpm-volumes-"))
            if include_volumes:
                for project in raw_projects:
                    names = self._volume_names(str(project["compose_project_name"]))
                    if not names:
                        continue
                    try:
                        services = self.compose.services(project, include_stats=False)
                        was_running = any(str(item.get("state") or "").lower() == "running" for item in services)
                    except ComposeError:
                        was_running = False
                    if was_running:
                        self.compose.stop_project(project)
                        stopped_projects.append(project)
                    for name in names:
                        info = self._volume_info(name)
                        mountpoint = Path(str(info.get("Mountpoint") or "")).resolve()
                        if not mountpoint.is_dir():
                            raise MigrationError(f"Volume {name} has no accessible local mountpoint")
                        tar_name = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16] + ".tar.gz"
                        tar_path = volume_temp_dir / tar_name
                        with tarfile.open(tar_path, "w:gz") as archive:
                            archive.add(mountpoint, arcname=".", recursive=True)
                        volumes.append({
                            "project": str(project["name"]),
                            "name": name,
                            "driver": str(info.get("Driver") or "local"),
                            "labels": info.get("Labels") or {},
                            "options": info.get("Options") or {},
                            "archive_path": f"volumes/{project['name']}/{tar_name}",
                            "size": tar_path.stat().st_size,
                            "_source": tar_path,
                        })

            for project in reversed(stopped_projects):
                try:
                    self.compose.start_project(project)
                except ComposeError as exc:
                    warnings.append(f"{project['name']}: exported, but automatic restart failed: {exc}")
            stopped_projects.clear()

            manifest = {
                "format": _ARCHIVE_FORMAT,
                "version": _ARCHIVE_VERSION,
                "created_at": utc_now(),
                "projects": [self._project_definition(item) for item in raw_projects],
                "files": [{k: v for k, v in item.items() if k != "_source"} for item in file_entries],
                "ssh": [{k: v for k, v in item.items() if k != "_source"} for item in ssh_entries],
                "volumes": [{k: v for k, v in item.items() if k != "_source"} for item in volumes],
                "warnings": warnings,
                "notes": [
                    "Repositories are not embedded; they are cloned again during import.",
                    "Imported projects are restored stopped with automatic updates disabled until reviewed.",
                ],
            }
            destination.parent.mkdir(parents=True, exist_ok=True)
            with pyzipper.AESZipFile(
                destination,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                encryption=pyzipper.WZ_AES,
            ) as archive:
                archive.setpassword(password.encode("utf-8"))
                archive.setencryption(pyzipper.WZ_AES, nbits=256)
                for entry in file_entries:
                    archive.write(entry["_source"], entry["archive_path"])
                for entry in ssh_entries:
                    archive.write(entry["_source"], entry["archive_path"])
                for entry in volumes:
                    archive.write(entry["_source"], entry["archive_path"], compress_type=zipfile.ZIP_STORED)
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                )
            return manifest
        finally:
            for project in reversed(stopped_projects):
                try:
                    self.compose.start_project(project)
                except ComposeError:
                    pass
            if volume_temp_dir:
                shutil.rmtree(volume_temp_dir, ignore_errors=True)
            self._lock.release()

    @staticmethod
    def _read_manifest(archive: pyzipper.AESZipFile) -> dict[str, Any]:
        try:
            raw = archive.read("manifest.json")
        except (KeyError, RuntimeError, ValueError) as exc:
            raise MigrationError("Cannot read manifest.json; check the archive password") from exc
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MigrationError("Migration manifest is invalid") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != _ARCHIVE_FORMAT:
            raise MigrationError("This is not a DPM migration archive")
        if int(manifest.get("version") or 0) != _ARCHIVE_VERSION:
            raise MigrationError("Unsupported DPM migration archive version")
        return manifest

    @staticmethod
    def _write_zip_entry(
        archive: pyzipper.AESZipFile,
        archive_path: str,
        destination: Path,
        expected_sha: str | None,
        mode: int,
    ) -> None:
        _safe_relative(archive_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(archive_path, "r") as source, destination.open("wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        if expected_sha and _sha256(destination) != expected_sha:
            destination.unlink(missing_ok=True)
            raise MigrationError(f"Checksum mismatch while restoring {destination}")
        os.chmod(destination, mode & 0o777)

    @staticmethod
    def _volume_exists(name: str) -> bool:
        result = subprocess.run(
            ["docker", "volume", "inspect", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    @classmethod
    def _preflight_volume(cls, item: dict[str, Any]) -> None:
        name = str(item.get("name") or "")
        if not name:
            raise MigrationError("Volume entry without a name")
        if not cls._volume_exists(name):
            return
        info = cls._volume_info(name)
        mountpoint = Path(str(info.get("Mountpoint") or "")).resolve()
        if not mountpoint.is_dir():
            raise MigrationError(f"Existing volume {name} has no accessible mountpoint")
        if any(mountpoint.iterdir()):
            raise MigrationError(f"Volume {name} already exists and is not empty")

    @staticmethod
    def _create_volume(item: dict[str, Any]) -> None:
        name = str(item["name"])
        if MigrationManager._volume_exists(name):
            return
        command = ["volume", "create", "--driver", str(item.get("driver") or "local")]
        labels = item.get("labels") or {}
        if isinstance(labels, dict):
            for key, value in labels.items():
                command.extend(["--label", f"{key}={value}"])
        options = item.get("options") or {}
        if isinstance(options, dict):
            for key, value in options.items():
                command.extend(["--opt", f"{key}={value}"])
        command.append(name)
        _run_docker(command)

    @staticmethod
    def _extract_volume_tar(tar_path: Path, destination: Path) -> None:
        with tarfile.open(tar_path, "r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                pure = PurePosixPath(member.name)
                if pure.is_absolute() or ".." in pure.parts:
                    raise MigrationError(f"Unsafe volume archive member: {member.name}")
                if not (member.isdir() or member.isfile()):
                    raise MigrationError(f"Unsupported special file in volume archive: {member.name}")
                target = (destination / Path(*pure.parts)).resolve()
                if not is_relative_to(target, destination.resolve()):
                    raise MigrationError(f"Unsafe volume archive member: {member.name}")
            archive.extractall(destination, members=members)

    def import_archive(self, source: Path, password: str) -> dict[str, Any]:
        if len(password) < 8:
            raise MigrationError("Archive password must contain at least 8 characters")
        if not self._lock.acquire(blocking=False):
            raise MigrationError("Another migration operation is already running")
        created_projects: list[int] = []
        try:
            try:
                archive = pyzipper.AESZipFile(source, "r")
            except (OSError, zipfile.BadZipFile) as exc:
                raise MigrationError("Uploaded file is not a readable ZIP archive") from exc
            with archive:
                archive.setpassword(password.encode("utf-8"))
                manifest = self._read_manifest(archive)
                projects = manifest.get("projects") or []
                files = manifest.get("files") or []
                ssh_entries = manifest.get("ssh") or []
                volumes = manifest.get("volumes") or []
                if not isinstance(projects, list) or not isinstance(files, list) or not isinstance(volumes, list):
                    raise MigrationError("Migration manifest has an invalid structure")

                existing = {row["name"] for row in self.db.fetchall("SELECT name FROM projects")}
                incoming_names = [str(item.get("name") or "") for item in projects if isinstance(item, dict)]
                duplicates = sorted(name for name in incoming_names if name in existing)
                if duplicates:
                    raise MigrationError("Projects already exist on this server: " + ", ".join(duplicates))
                if len(set(incoming_names)) != len(incoming_names) or any(not name for name in incoming_names):
                    raise MigrationError("Migration archive contains duplicate or empty project names")
                for item in volumes:
                    if not isinstance(item, dict):
                        raise MigrationError("Invalid volume entry in migration archive")
                    self._preflight_volume(item)

                ssh_dir = self.settings.data_dir / ".ssh"
                if ssh_entries:
                    ssh_dir.mkdir(parents=True, exist_ok=True)
                    os.chmod(ssh_dir, 0o700)
                    for item in ssh_entries:
                        if not isinstance(item, dict):
                            raise MigrationError("Invalid SSH entry in migration archive")
                        relative = _safe_relative(str(item.get("relative_path") or ""))
                        destination = (ssh_dir / relative).resolve()
                        if not is_relative_to(destination, ssh_dir.resolve()):
                            raise MigrationError("Unsafe SSH destination")
                        self._write_zip_entry(
                            archive,
                            str(item["archive_path"]),
                            destination,
                            str(item.get("sha256") or "") or None,
                            int(item.get("mode") or 0o600),
                        )

                project_file_entries: list[dict[str, Any]] = []
                for item in files:
                    if not isinstance(item, dict):
                        raise MigrationError("Invalid config file entry in migration archive")
                    if item.get("location") == "repository":
                        project_file_entries.append(item)
                        continue
                    destination = Path(str(item.get("destination") or "")).resolve()
                    if not is_relative_to(destination, Path("/etc")):
                        raise MigrationError(f"Refusing to restore external config outside /etc: {destination}")
                    self._write_zip_entry(
                        archive,
                        str(item["archive_path"]),
                        destination,
                        str(item.get("sha256") or "") or None,
                        int(item.get("mode") or 0o600),
                    )

                imported: dict[str, int] = {}
                for item in projects:
                    if not isinstance(item, dict):
                        raise MigrationError("Invalid project entry in migration archive")
                    project = self.projects.add_project(
                        repository_url=str(item["repository_url"]),
                        branch=str(item.get("branch") or "master"),
                        name=str(item["name"]),
                        compose_file=str(item.get("compose_file") or "compose.yml"),
                        env_file=str(item["env_file"]) if item.get("env_file") else None,
                        compose_project_name=str(item.get("compose_project_name") or item["name"]),
                        auto_update=False,
                        poll_interval=int(item.get("poll_interval") or self.settings.poll_interval),
                        environment=None,
                        schedule_initial_deploy=False,
                        desired_state="stopped",
                    )
                    project_id = int(project["id"])
                    created_projects.append(project_id)
                    imported[str(item["name"])] = project_id
                    self.projects.prepare_checkout(project_id)

                for item in project_file_entries:
                    project_name = str(item.get("project") or "")
                    project_id = imported.get(project_name)
                    if not project_id:
                        raise MigrationError(f"Config file references unknown project {project_name}")
                    project = self.projects.raw_project(project_id)
                    repository = Path(project["repo_path"]).resolve()
                    relative = _safe_relative(str(item.get("destination") or ""))
                    destination = (repository / relative).resolve()
                    if not is_relative_to(destination, repository):
                        raise MigrationError("Unsafe repository config destination")
                    self._write_zip_entry(
                        archive,
                        str(item["archive_path"]),
                        destination,
                        str(item.get("sha256") or "") or None,
                        int(item.get("mode") or 0o600),
                    )

                restored_volumes: list[str] = []
                with tempfile.TemporaryDirectory(prefix="dpm-import-") as tmp:
                    temp_root = Path(tmp)
                    for item in volumes:
                        self._create_volume(item)
                        info = self._volume_info(str(item["name"]))
                        mountpoint = Path(str(info.get("Mountpoint") or "")).resolve()
                        if not mountpoint.is_dir():
                            raise MigrationError(f"Volume {item['name']} has no accessible local mountpoint")
                        tar_path = temp_root / (hashlib.sha256(str(item["name"]).encode()).hexdigest()[:16] + ".tar.gz")
                        archive_name = str(item["archive_path"])
                        _safe_relative(archive_name)
                        with archive.open(archive_name, "r") as zipped, tar_path.open("wb") as target:
                            shutil.copyfileobj(zipped, target, length=1024 * 1024)
                        self._extract_volume_tar(tar_path, mountpoint)
                        restored_volumes.append(str(item["name"]))

            return {
                "projects": [{"name": name, "id": project_id} for name, project_id in imported.items()],
                "files": len(files),
                "ssh_files": len(ssh_entries),
                "volumes": restored_volumes,
                "warnings": list(manifest.get("warnings") or []) + [
                    "Imported projects are stopped and automatic updates are disabled.",
                    "Review host-specific URLs and domains before deploying.",
                ],
            }
        except (ProjectError, ComposeError) as exc:
            raise MigrationError(str(exc)) from exc
        finally:
            self._lock.release()
