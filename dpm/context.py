from __future__ import annotations

from dataclasses import dataclass

from .compose import ComposeRuntime
from .config import Settings
from .db import Database
from .gitops import GitRepository
from .projects import ProjectManager
from .scheduler import UpdateScheduler


@dataclass
class DpmContext:
    settings: Settings
    db: Database
    compose: ComposeRuntime
    projects: ProjectManager
    scheduler: UpdateScheduler

    @classmethod
    def create(cls, settings: Settings, *, start_background: bool = True) -> "DpmContext":
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.projects_dir.mkdir(parents=True, exist_ok=True)
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        db = Database(settings.database_path)
        compose = ComposeRuntime(settings)
        projects = ProjectManager(settings, db, GitRepository(), compose)
        scheduler = UpdateScheduler(db, projects)
        context = cls(settings, db, compose, projects, scheduler)
        if start_background:
            scheduler.start()
        return context

    def shutdown(self) -> None:
        self.scheduler.shutdown()
        self.projects.shutdown()
