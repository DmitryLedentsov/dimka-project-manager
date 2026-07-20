from __future__ import annotations

import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from .components import ComponentError, ComponentManager
from .config import Settings
from .db import Database
from .gitops import GitError, GitRepository
from .utils import repository_name, slugify, utc_now


class ProjectError(RuntimeError):
    pass


class ProjectManagerBase:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        git: GitRepository,
        components: ComponentManager,
    ) -> None:
        self.settings = settings
        self.db = db
        self.git = git
        self.components = components
        self.supervisor = components  # compatibility for older internal calls
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="dpm-deploy")
        self._locks: dict[int, threading.Lock] = {}
        self._futures: dict[int, Future[Any]] = {}
        self._guard = threading.RLock()

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def add_project(
        self,
        repository_url: str,
        branch: str = "master",
        name: str | None = None,
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
        project_root = self.settings.projects_dir / project_name
        repo_path = project_root / "repository"
        now = utc_now()
        try:
            project_id = self.db.execute(
                """
                INSERT INTO projects (
                    name, repository_url, branch, repo_path, auto_update,
                    poll_interval, desired_state, deploy_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', 'queued', ?, ?)
                """,
                [
                    project_name,
                    repository_url,
                    branch,
                    str(repo_path),
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
        project_root.mkdir(parents=True, exist_ok=True)
        (project_root / "logs").mkdir(parents=True, exist_ok=True)
        project = self.get_project(project_id)
        self.schedule_deploy(project_id, reason="initial install", force=True)
        return project

    def get_project(self, project_id: int) -> dict[str, Any]:
        project = self.db.fetchone("SELECT * FROM projects WHERE id = ?", [project_id])
        if not project:
            raise ProjectError("Project not found")
        return self.enrich_project(project)

    def list_projects(self) -> list[dict[str, Any]]:
        projects = self.db.fetchall("SELECT * FROM projects ORDER BY name")
        return [self.enrich_project(project) for project in projects]

    def enrich_project(self, project: dict[str, Any]) -> dict[str, Any]:
        item = dict(project)
        item["auto_update"] = bool(item.get("auto_update"))
        item["desired_state"] = str(item.get("desired_state") or "running")
        components = self.components.list_components(int(item["id"]))
        summary = self.components.project_summary(int(item["id"]))
        item["components"] = components
        item["component_count"] = summary["total"]
        item["ready_count"] = summary["ready"]
        item["failed_count"] = summary["failed"]
        # Compatibility fields consumed by the initial CLI/UI generation.
        item["service_count"] = summary["total"]
        item["running_count"] = summary["ready"]
        item["deploying"] = self.is_busy(int(item["id"]))
        item["update_available"] = bool(
            item.get("remote_commit")
            and item.get("remote_commit") != item.get("deployed_commit")
        )
        latest = self.db.fetchone(
            "SELECT * FROM deployments WHERE project_id = ? ORDER BY id DESC LIMIT 1",
            [item["id"]],
        )
        item["latest_deployment"] = latest

        if item["deploying"] or item.get("deploy_status") == "deploying":
            actual_state = "deploying"
        elif item["desired_state"] == "stopped" and summary["ready"] == 0:
            # Runtime state follows an explicit operator Stop. Deployment failures
            # remain available separately through deploy_status/last_error.
            actual_state = "stopped"
        elif item.get("deploy_status") in {"failed", "check_failed"}:
            actual_state = "failed"
        elif summary["failed"]:
            actual_state = "degraded"
        elif summary["total"] and summary["ready"] == summary["total"]:
            actual_state = "running"
        elif summary["total"] == 0:
            actual_state = "empty"
        else:
            actual_state = "degraded"
        item["actual_state"] = actual_state
        return item

    def start_project(self, project_id: int) -> dict[str, Any]:
        project = self.get_project(project_id)
        if not project.get("deployed_commit"):
            raise ProjectError("Project has not been deployed successfully yet")
        self.db.update(
            "projects",
            project_id,
            {"desired_state": "running", "updated_at": utc_now()},
        )
        try:
            self.components.start_project(project_id)
        except ComponentError as exc:
            raise ProjectError(str(exc)) from exc
        return self.get_project(project_id)

    def stop_project(self, project_id: int) -> dict[str, Any]:
        self.get_project(project_id)
        self.db.update(
            "projects",
            project_id,
            {"desired_state": "stopped", "updated_at": utc_now()},
        )
        self.components.stop_project(project_id)
        return self.get_project(project_id)

    def is_busy(self, project_id: int) -> bool:
        with self._guard:
            future = self._futures.get(project_id)
            return bool(future and not future.done())

    def schedule_deploy(
        self,
        project_id: int,
        *,
        reason: str = "manual",
        force: bool = False,
    ) -> bool:
        with self._guard:
            existing = self._futures.get(project_id)
            if existing and not existing.done():
                return False
            future = self._executor.submit(self.deploy_project, project_id, reason, force)
            self._futures[project_id] = future
            return True

    def schedule_check(self, project_id: int, *, force_deploy: bool = False) -> bool:
        with self._guard:
            existing = self._futures.get(project_id)
            if existing and not existing.done():
                return False
            future = self._executor.submit(self.check_for_updates, project_id, force_deploy)
            self._futures[project_id] = future
            return True

    def _project_lock(self, project_id: int) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(project_id, threading.Lock())

    def check_for_updates(self, project_id: int, force_deploy: bool = False) -> None:
        with self._project_lock(project_id):
            project = self.db.fetchone("SELECT * FROM projects WHERE id = ?", [project_id])
            if not project:
                return
            now = utc_now()
            try:
                remote_sha = self.git.remote_sha(project["repository_url"], project["branch"])
                self.db.update(
                    "projects",
                    project_id,
                    {
                        "remote_commit": remote_sha,
                        "last_checked_at": now,
                        "updated_at": now,
                    },
                )
            except GitError as exc:
                self.db.update(
                    "projects",
                    project_id,
                    {
                        "last_checked_at": now,
                        "last_error": str(exc),
                        "deploy_status": "check_failed",
                        "deploy_stage": "checking",
                        "updated_at": now,
                    },
                )
                return

            should_deploy = force_deploy or (
                remote_sha != project.get("deployed_commit")
                and remote_sha != project.get("attempted_commit")
            )
        if should_deploy:
            self.deploy_project(project_id, "new commit", force_deploy, known_remote=remote_sha)
