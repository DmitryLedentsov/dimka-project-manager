from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .gitops import GitError
from .manifest import ManifestError, ProjectManifest, ServiceDefinition, load_manifest
from .projects_base import ProjectError
from .supervisor import ServiceError
from .utils import slugify, tail_file, utc_now


class DeploymentMixin:
    def deploy_project(
        self,
        project_id: int,
        reason: str = "manual",
        force: bool = False,
        *,
        known_remote: str | None = None,
    ) -> None:
        with self._project_lock(project_id):
            project = self.db.fetchone("SELECT * FROM projects WHERE id = ?", [project_id])
            if not project:
                return

            project_root = Path(project["repo_path"]).parent
            deployment_log = project_root / "logs" / f"deploy-{utc_now().replace(':', '-')}.log"
            deployment_log.parent.mkdir(parents=True, exist_ok=True)
            deployment_id = self.db.execute(
                """
                INSERT INTO deployments (project_id, commit_sha, status, stage, started_at, log_path)
                VALUES (?, ?, 'running', 'fetching', ?, ?)
                """,
                [project_id, known_remote, utc_now(), str(deployment_log)],
            )

            try:
                self._set_deploy_state(project_id, "deploying", "fetching", None)
                self._write_deploy_log(deployment_log, f"deployment started ({reason})")
                self.git.ensure_checkout(
                    project["repository_url"],
                    project["branch"],
                    Path(project["repo_path"]),
                )
                commit = self.git.current_commit(Path(project["repo_path"]))
                self.db.update(
                    "projects",
                    project_id,
                    {
                        "remote_commit": commit.sha,
                        "attempted_commit": commit.sha,
                        "commit_message": commit.message,
                        "commit_time": commit.timestamp,
                        "updated_at": utc_now(),
                    },
                )
                self.db.update("deployments", deployment_id, {"commit_sha": commit.sha})
                self._write_deploy_log(deployment_log, f"checked out {commit.sha[:12]} {commit.message}")

                self._set_deploy_state(project_id, "deploying", "reading_manifest", None)
                manifest = load_manifest(Path(project["repo_path"]))
                self._write_deploy_log(
                    deployment_log,
                    f"manifest loaded: {len(manifest.services)} service(s)",
                )

                # A failed first build must still expose the declared services in the
                # dashboard. Only missing records are added here: existing running
                # services are not changed or removed until the new build succeeds.
                self._register_missing_services(project, manifest)

                self._set_deploy_state(project_id, "deploying", "building", None)
                self.db.update("deployments", deployment_id, {"stage": "building"})
                self._run_build(project, manifest, deployment_log)

                self._set_deploy_state(project_id, "deploying", "stopping", None)
                self.db.update("deployments", deployment_id, {"stage": "stopping"})
                self.supervisor.stop_project(project_id)

                # The artifacts are valid now, so apply removals and definition changes.
                self._sync_services(project, manifest)

                self._set_deploy_state(project_id, "deploying", "starting", None)
                self.db.update("deployments", deployment_id, {"stage": "starting"})
                self.supervisor.start_project(project_id)

                now = utc_now()
                self.db.update(
                    "projects",
                    project_id,
                    {
                        "deployed_commit": commit.sha,
                        "last_deployed_at": now,
                        "deploy_status": "running",
                        "deploy_stage": None,
                        "last_error": None,
                        "updated_at": now,
                    },
                )
                self.db.update(
                    "deployments",
                    deployment_id,
                    {
                        "status": "success",
                        "stage": "complete",
                        "finished_at": now,
                        "error": None,
                    },
                )
                self._write_deploy_log(deployment_log, "deployment completed successfully")
            except (GitError, ManifestError, ServiceError, ProjectError, OSError) as exc:
                stage = self.db.fetchone(
                    "SELECT deploy_stage FROM projects WHERE id = ?", [project_id]
                )
                stage_name = (stage or {}).get("deploy_stage") or "unknown"
                now = utc_now()
                self.db.update(
                    "projects",
                    project_id,
                    {
                        "deploy_status": "failed",
                        "last_error": str(exc),
                        "updated_at": now,
                    },
                )
                self.db.update(
                    "deployments",
                    deployment_id,
                    {
                        "status": "failed",
                        "stage": stage_name,
                        "finished_at": now,
                        "error": str(exc),
                    },
                )
                self._write_deploy_log(
                    deployment_log, f"deployment failed at {stage_name}: {exc}"
                )
            except Exception as exc:
                now = utc_now()
                self.db.update(
                    "projects",
                    project_id,
                    {
                        "deploy_status": "failed",
                        "last_error": f"Unexpected error: {exc}",
                        "updated_at": now,
                    },
                )
                self.db.update(
                    "deployments",
                    deployment_id,
                    {
                        "status": "failed",
                        "finished_at": now,
                        "error": f"Unexpected error: {exc}",
                    },
                )
                self._write_deploy_log(deployment_log, f"unexpected failure: {exc}")

    def _set_deploy_state(
        self,
        project_id: int,
        status: str,
        stage: str | None,
        error: str | None,
    ) -> None:
        self.db.update(
            "projects",
            project_id,
            {
                "deploy_status": status,
                "deploy_stage": stage,
                "last_error": error,
                "updated_at": utc_now(),
            },
        )

    @staticmethod
    def _write_deploy_log(path: Path, message: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{utc_now()}] [deploy] {message}\n")

    @staticmethod
    def _build_failure_detail(path: Path) -> str | None:
        lines = [line.strip() for line in tail_file(path, 80).splitlines() if line.strip()]
        if not lines:
            return None

        preferred_markers = (
            "ERROR:",
            "[ERROR]",
            "npm ERR!",
            "fatal:",
            "permission denied",
            "not found",
            "is required",
            "is missing",
        )
        for line in reversed(lines):
            lowered = line.lower()
            if any(marker.lower() in lowered for marker in preferred_markers):
                return line[:420]

        for line in reversed(lines):
            if "[deploy]" not in line:
                return line[:420]
        return None

    def _run_build(
        self,
        project: dict[str, Any],
        manifest: ProjectManifest,
        log_path: Path,
    ) -> None:
        repository = Path(project["repo_path"])
        environment = os.environ.copy()
        environment.update(
            {
                "DPM_PROJECT": project["name"],
                "DPM_REPOSITORY": str(repository),
                "CI": "1",
            }
        )
        for command in manifest.build_commands:
            self._write_deploy_log(log_path, f"build: {command}")
            with log_path.open("a", encoding="utf-8", buffering=1) as log_file:
                process = subprocess.Popen(
                    ["/bin/bash", "-lc", command],
                    cwd=repository,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    return_code = process.wait(timeout=1800)
                except subprocess.TimeoutExpired as exc:
                    try:
                        os.killpg(process.pid, 9)
                    except ProcessLookupError:
                        pass
                    raise ProjectError(f"Build timed out: {command}") from exc
            if return_code != 0:
                detail = self._build_failure_detail(log_path)
                message = f"Build command exited with {return_code}: {command}"
                if detail:
                    message += f" — {detail}"
                raise ProjectError(message)

    def _service_values(
        self,
        project: dict[str, Any],
        definition: ServiceDefinition,
        now: str,
    ) -> dict[str, Any]:
        log_path = self.settings.log_dir / project["name"] / f"{slugify(definition.name)}.log"
        return {
            "command_json": json.dumps(definition.command_for_storage()),
            "working_directory": definition.working_directory,
            "environment_json": json.dumps(definition.environment),
            "environment_file": definition.environment_file,
            "restart_policy": definition.restart_policy,
            "healthcheck_json": json.dumps(definition.healthcheck) if definition.healthcheck else None,
            "depends_on_json": json.dumps(definition.depends_on),
            "enabled": 1 if definition.enabled else 0,
            "log_path": str(log_path),
            "updated_at": now,
        }

    def _insert_service(
        self,
        project: dict[str, Any],
        definition: ServiceDefinition,
        values: dict[str, Any],
        now: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO services (
                project_id, name, command_json, working_directory,
                environment_json, environment_file, restart_policy,
                healthcheck_json, depends_on_json, enabled, status,
                log_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'stopped', ?, ?, ?)
            """,
            [
                project["id"],
                definition.name,
                values["command_json"],
                definition.working_directory,
                values["environment_json"],
                definition.environment_file,
                definition.restart_policy,
                values["healthcheck_json"],
                values["depends_on_json"],
                values["enabled"],
                values["log_path"],
                now,
                now,
            ],
        )

    def _register_missing_services(
        self,
        project: dict[str, Any],
        manifest: ProjectManifest,
    ) -> None:
        existing_names = {
            row["name"]
            for row in self.db.fetchall(
                "SELECT name FROM services WHERE project_id = ?", [project["id"]]
            )
        }
        now = utc_now()
        for definition in manifest.services:
            if definition.name in existing_names:
                continue
            values = self._service_values(project, definition, now)
            self._insert_service(project, definition, values, now)

    def _sync_services(self, project: dict[str, Any], manifest: ProjectManifest) -> None:
        now = utc_now()
        existing = {
            row["name"]: row
            for row in self.db.fetchall(
                "SELECT * FROM services WHERE project_id = ?", [project["id"]]
            )
        }
        configured_names = {service.name for service in manifest.services}

        for name, row in existing.items():
            if name not in configured_names:
                self.supervisor.delete_service(row["id"])

        for definition in manifest.services:
            values = self._service_values(project, definition, now)
            if definition.name in existing:
                self.db.update("services", existing[definition.name]["id"], values)
            else:
                self._insert_service(project, definition, values, now)

    def delete_project(self, project_id: int, *, purge: bool = True) -> None:
        project = self.db.fetchone("SELECT * FROM projects WHERE id = ?", [project_id])
        if not project:
            raise ProjectError("Project not found")
        self.supervisor.stop_project(project_id)
        self.db.execute("DELETE FROM projects WHERE id = ?", [project_id])
        if purge:
            shutil.rmtree(Path(project["repo_path"]).parent, ignore_errors=True)

    def latest_deploy_log(self, project_id: int, lines: int = 200) -> str:
        deployment = self.db.fetchone(
            "SELECT log_path FROM deployments WHERE project_id = ? ORDER BY id DESC LIMIT 1",
            [project_id],
        )
        if not deployment:
            return ""
        return tail_file(Path(deployment["log_path"]), lines)
