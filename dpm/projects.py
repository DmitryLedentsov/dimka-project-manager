from __future__ import annotations

import re
import shutil
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .compose import ComposeError, ComposeRuntime
from .config import Settings
from .db import Database
from .gitops import GitError, GitRepository
from .utils import repository_name, slugify, tail_file, utc_now


class ProjectError(RuntimeError):
    pass


class ProjectManager:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        git: GitRepository,
        compose: ComposeRuntime,
    ) -> None:
        self.settings = settings
        self.db = db
        self.git = git
        self.compose = compose
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="dpm-deploy")
        self._locks: dict[int, threading.Lock] = {}
        self._futures: dict[int, Future[Any]] = {}
        self._guard = threading.RLock()

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _lock(self, project_id: int) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(project_id, threading.Lock())

    def is_busy(self, project_id: int) -> bool:
        with self._guard:
            future = self._futures.get(project_id)
            return bool(future and not future.done())

    def _schedule(self, project_id: int, function: Any, *args: Any) -> bool:
        with self._guard:
            existing = self._futures.get(project_id)
            if existing and not existing.done():
                return False
            self._futures[project_id] = self._executor.submit(function, project_id, *args)
            return True

    def add_project(
        self,
        repository_url: str,
        branch: str = "master",
        name: str | None = None,
        compose_file: str = "compose.yml",
        env_file: str | None = None,
        compose_project_name: str | None = None,
        auto_update: bool = True,
        poll_interval: int | None = None,
    ) -> dict[str, Any]:
        repository_url = repository_url.strip()
        if not repository_url or "\n" in repository_url:
            raise ProjectError("Repository URL is required")
        branch = branch.strip() or "master"
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,160}", branch) or ".." in branch:
            raise ProjectError("Branch contains unsupported characters")
        project_name = slugify(name) if name else repository_name(repository_url)
        compose_name = slugify(compose_project_name or project_name).replace(".", "-")
        compose_file = compose_file.strip() or "compose.yml"
        project_root = self.settings.projects_dir / project_name
        repo_path = project_root / "repository"
        now = utc_now()
        try:
            project_id = self.db.execute(
                """
                INSERT INTO projects (
                    name, repository_url, branch, repo_path, compose_file,
                    compose_project_name, env_file, auto_update, poll_interval,
                    desired_state, deploy_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', 'queued', ?, ?)
                """,
                [
                    project_name,
                    repository_url,
                    branch,
                    str(repo_path),
                    compose_file,
                    compose_name,
                    env_file.strip() if env_file else None,
                    1 if auto_update else 0,
                    max(15, poll_interval or self.settings.poll_interval),
                    now,
                    now,
                ],
            )
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise ProjectError(f"Project '{project_name}' already exists") from exc
            raise
        (project_root / "logs").mkdir(parents=True, exist_ok=True)
        project = self.get_project(project_id)
        self.schedule_deploy(project_id, force=True, reason="initial install")
        return project

    def raw_project(self, project_id: int) -> dict[str, Any]:
        project = self.db.fetchone("SELECT * FROM projects WHERE id = ?", [project_id])
        if not project:
            raise ProjectError("Project not found")
        return project

    def get_project(self, project_id: int) -> dict[str, Any]:
        return self.enrich_project(self.raw_project(project_id))

    def list_projects(self) -> list[dict[str, Any]]:
        return [self.enrich_project(item) for item in self.db.fetchall("SELECT * FROM projects ORDER BY name")]

    def enrich_project(self, project: dict[str, Any]) -> dict[str, Any]:
        item = dict(project)
        item["auto_update"] = bool(item.get("auto_update"))
        item["desired_state"] = str(item.get("desired_state") or "running")
        item["deploying"] = self.is_busy(int(item["id"]))
        item["update_available"] = bool(
            item.get("remote_commit") and item.get("remote_commit") != item.get("deployed_commit")
        )
        try:
            env_path = self.compose.env_path(item)
            item["resolved_env_file"] = str(env_path) if env_path else None
            services = self.compose.services(item)
            compose_error = None
        except ComposeError as exc:
            services = []
            compose_error = str(exc)
            item["resolved_env_file"] = None
        item["services"] = services
        item["service_count"] = len(services)
        item["ready_count"] = sum(
            1 for service in services if service["status"] in {"running", "healthy", "completed"}
        )
        item["failed_count"] = sum(
            1 for service in services if service["status"] in {"failed", "unhealthy", "invalid"}
        )
        item["compose_error"] = compose_error
        item["latest_deployment"] = self.db.fetchone(
            "SELECT * FROM deployments WHERE project_id = ? ORDER BY id DESC LIMIT 1",
            [item["id"]],
        )
        if item["deploying"] or item.get("deploy_status") == "deploying":
            actual = "deploying"
        elif item["desired_state"] == "stopped" and item["ready_count"] == 0:
            actual = "stopped"
        elif compose_error and not item.get("deployed_commit"):
            actual = "failed"
        elif item["failed_count"]:
            actual = "degraded" if item["ready_count"] else "failed"
        elif services and item["ready_count"] == len(services):
            actual = "running"
        elif item.get("deploy_status") in {"failed", "check_failed"}:
            actual = "failed" if not item["ready_count"] else "degraded"
        elif not services:
            actual = "empty"
        else:
            actual = "degraded"
        item["actual_state"] = actual
        return item

    def service(self, project_id: int, service_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        service_name = self.compose.safe_service_name(service_name)
        project = self.get_project(project_id)
        service = next((item for item in project["services"] if item["name"] == service_name), None)
        if not service:
            raise ProjectError("Compose service not found")
        return project, service

    def schedule_check(self, project_id: int) -> bool:
        self.raw_project(project_id)
        return self._schedule(project_id, self.check_for_updates)

    def schedule_deploy(self, project_id: int, *, force: bool, reason: str) -> bool:
        self.raw_project(project_id)
        return self._schedule(project_id, self.deploy_project, force, reason)

    def check_for_updates(self, project_id: int) -> None:
        with self._lock(project_id):
            project = self.raw_project(project_id)
            now = utc_now()
            try:
                remote_sha = self.git.remote_sha(project["repository_url"], project["branch"])
                self.db.update(
                    "projects",
                    project_id,
                    {"remote_commit": remote_sha, "last_checked_at": now, "updated_at": now},
                )
            except GitError as exc:
                self.db.update(
                    "projects",
                    project_id,
                    {
                        "last_checked_at": now,
                        "deploy_status": "check_failed",
                        "deploy_stage": "checking",
                        "last_error": str(exc),
                        "updated_at": now,
                    },
                )
                return
            if remote_sha == project.get("deployed_commit") or remote_sha == project.get("attempted_commit"):
                return
            self._deploy_locked(project_id, False, "new commit", known_remote=remote_sha)

    def deploy_project(self, project_id: int, force: bool, reason: str) -> None:
        with self._lock(project_id):
            self._deploy_locked(project_id, force, reason)

    def _deploy_locked(
        self,
        project_id: int,
        force: bool,
        reason: str,
        *,
        known_remote: str | None = None,
    ) -> None:
        project = self.raw_project(project_id)
        project_root = Path(project["repo_path"]).parent
        log_path = project_root / "logs" / f"deploy-{utc_now().replace(':', '-')}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        deployment_id = self.db.execute(
            """
            INSERT INTO deployments (project_id, commit_sha, status, stage, started_at, log_path)
            VALUES (?, ?, 'running', 'fetching', ?, ?)
            """,
            [project_id, known_remote, utc_now(), str(log_path)],
        )
        try:
            self._state(project_id, "deploying", "fetching", None)
            with log_path.open("a", encoding="utf-8", buffering=1) as log:
                log.write(f"[{utc_now()}] deployment started ({reason})\n")
                self.git.ensure_checkout(
                    project["repository_url"], project["branch"], Path(project["repo_path"])
                )
                commit = self.git.current_commit(Path(project["repo_path"]))
                if not force and commit.sha == project.get("deployed_commit"):
                    log.write(f"[{utc_now()}] commit {commit.sha[:12]} is already deployed\n")
                    self.db.update(
                        "deployments",
                        deployment_id,
                        {"commit_sha": commit.sha, "status": "skipped", "stage": "complete", "finished_at": utc_now()},
                    )
                    self.db.update(
                        "projects",
                        project_id,
                        {"remote_commit": commit.sha, "deploy_status": project.get("desired_state") or "running", "deploy_stage": None, "last_error": None, "updated_at": utc_now()},
                    )
                    return
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
                log.write(f"[{utc_now()}] checked out {commit.sha[:12]} {commit.message}\n")
                project = self.raw_project(project_id)

                self._state(project_id, "deploying", "validating", None)
                self.db.update("deployments", deployment_id, {"stage": "validating"})
                config = self.compose.validate(project)
                log.write(f"[{utc_now()}] compose config valid: {len(config['services'])} service(s)\n")

                self._state(project_id, "deploying", "building", None)
                self.db.update("deployments", deployment_id, {"stage": "building"})
                self.compose.build(project, log, pull=True)

                desired_state = str(project.get("desired_state") or "running")
                if desired_state == "running":
                    self._state(project_id, "deploying", "applying", None)
                    self.db.update("deployments", deployment_id, {"stage": "applying"})
                    self.compose.apply(project, log)
                else:
                    log.write(f"[{utc_now()}] project is stopped; images built without starting containers\n")

                now = utc_now()
                final_status = "running" if desired_state == "running" else "stopped"
                self.db.update(
                    "projects",
                    project_id,
                    {
                        "deployed_commit": commit.sha,
                        "last_deployed_at": now,
                        "deploy_status": final_status,
                        "deploy_stage": None,
                        "last_error": None,
                        "updated_at": now,
                    },
                )
                self.db.update(
                    "deployments",
                    deployment_id,
                    {"status": "success", "stage": "complete", "finished_at": now, "error": None},
                )
                log.write(f"[{now}] deployment completed successfully\n")
        except (GitError, ComposeError, OSError) as exc:
            current = self.raw_project(project_id)
            stage = current.get("deploy_stage") or "unknown"
            now = utc_now()
            self.db.update(
                "projects",
                project_id,
                {"deploy_status": "failed", "last_error": str(exc), "updated_at": now},
            )
            self.db.update(
                "deployments",
                deployment_id,
                {"status": "failed", "stage": stage, "finished_at": now, "error": str(exc)},
            )
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"[{now}] deployment failed at {stage}: {exc}\n")

    def _state(self, project_id: int, status: str, stage: str | None, error: str | None) -> None:
        self.db.update(
            "projects",
            project_id,
            {"deploy_status": status, "deploy_stage": stage, "last_error": error, "updated_at": utc_now()},
        )

    def start_project(self, project_id: int) -> dict[str, Any]:
        project = self.raw_project(project_id)
        if not project.get("deployed_commit"):
            raise ProjectError("Project has not been deployed successfully yet")
        self.db.update("projects", project_id, {"desired_state": "running", "updated_at": utc_now()})
        try:
            self.compose.start_project(self.raw_project(project_id))
        except ComposeError as exc:
            raise ProjectError(str(exc)) from exc
        return self.get_project(project_id)

    def stop_project(self, project_id: int) -> dict[str, Any]:
        project = self.raw_project(project_id)
        self.db.update("projects", project_id, {"desired_state": "stopped", "updated_at": utc_now()})
        try:
            self.compose.stop_project(project)
        except ComposeError as exc:
            raise ProjectError(str(exc)) from exc
        return self.get_project(project_id)

    def service_action(self, project_id: int, service_name: str, action: str) -> dict[str, Any]:
        project = self.raw_project(project_id)
        service_name = self.compose.safe_service_name(service_name)
        if action in {"start", "restart"} and str(project.get("desired_state")) == "stopped":
            raise ProjectError("Project is stopped; start the project first")
        try:
            if action == "start":
                self.compose.start_service(project, service_name)
            elif action == "stop":
                self.compose.stop_service(project, service_name)
            elif action == "restart":
                self.compose.restart_service(project, service_name)
            else:
                raise ProjectError("Unknown service action")
        except ComposeError as exc:
            raise ProjectError(str(exc)) from exc
        _, service = self.service(project_id, service_name)
        return service

    def service_logs(self, project_id: int, service_name: str, lines: int) -> str:
        project = self.raw_project(project_id)
        service_name = self.compose.safe_service_name(service_name)
        try:
            return self.compose.service_logs(project, service_name, lines)
        except ComposeError as exc:
            raise ProjectError(str(exc)) from exc

    def update_project(self, project_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.raw_project(project_id)
        values: dict[str, Any] = {"updated_at": utc_now()}
        if "auto_update" in payload:
            values["auto_update"] = 1 if payload["auto_update"] else 0
        if "poll_interval" in payload:
            values["poll_interval"] = max(15, int(payload["poll_interval"]))
        if "compose_file" in payload:
            values["compose_file"] = str(payload["compose_file"]).strip() or "compose.yml"
        if "env_file" in payload:
            values["env_file"] = str(payload["env_file"]).strip() or None
        if "compose_project_name" in payload:
            values["compose_project_name"] = slugify(str(payload["compose_project_name"])).replace(".", "-")
        self.db.update("projects", project_id, values)
        return self.get_project(project_id)

    def delete_project(self, project_id: int, *, purge: bool = True) -> None:
        project = self.raw_project(project_id)
        self.compose.remove_project(project)
        self.db.execute("DELETE FROM projects WHERE id = ?", [project_id])
        if purge:
            shutil.rmtree(Path(project["repo_path"]).parent, ignore_errors=True)

    def latest_deploy_log(self, project_id: int, lines: int = 250) -> str:
        self.raw_project(project_id)
        deployment = self.db.fetchone(
            "SELECT log_path FROM deployments WHERE project_id = ? ORDER BY id DESC LIMIT 1",
            [project_id],
        )
        if not deployment:
            return ""
        return tail_file(Path(deployment["log_path"]), lines)
