from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .db import Database
from .gitops import GitRepository
from .projects import ProjectManager
from .scheduler import UpdateScheduler
from .supervisor import ServiceSupervisor


@dataclass
class DpmContext:
    settings: Settings
    db: Database
    supervisor: ServiceSupervisor
    projects: ProjectManager
    scheduler: UpdateScheduler

    @classmethod
    def create(cls, settings: Settings, *, start_background: bool = True) -> "DpmContext":
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.projects_dir.mkdir(parents=True, exist_ok=True)
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        db = Database(settings.database_path)
        supervisor = ServiceSupervisor(db)
        projects = ProjectManager(settings, db, GitRepository(), supervisor)
        scheduler = UpdateScheduler(db, projects)
        context = cls(settings, db, supervisor, projects, scheduler)
        if start_background:
            supervisor.start_monitoring()
            scheduler.start()
        return context

    def shutdown(self) -> None:
        self.scheduler.shutdown()
        self.projects.shutdown()
        self.supervisor.shutdown()
