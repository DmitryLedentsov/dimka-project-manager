from __future__ import annotations

from dataclasses import dataclass

from .components import ComponentManager
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
    components: ComponentManager
    projects: ProjectManager
    scheduler: UpdateScheduler

    @property
    def supervisor(self) -> ComponentManager:
        """Compatibility alias for the first public DPM API."""
        return self.components

    @classmethod
    def create(cls, settings: Settings, *, start_background: bool = True) -> "DpmContext":
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.projects_dir.mkdir(parents=True, exist_ok=True)
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        db = Database(settings.database_path)
        process_supervisor = ServiceSupervisor(db)
        components = ComponentManager(settings, db, process_supervisor)
        projects = ProjectManager(settings, db, GitRepository(), components)
        scheduler = UpdateScheduler(db, projects)
        context = cls(settings, db, components, projects, scheduler)
        if start_background:
            components.start_monitoring()
            scheduler.start()
        return context

    def shutdown(self) -> None:
        self.scheduler.shutdown()
        self.projects.shutdown()
        self.components.shutdown()
