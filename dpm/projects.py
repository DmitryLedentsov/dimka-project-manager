from __future__ import annotations

from .projects_base import ProjectError, ProjectManagerBase
from .projects_deploy import DeploymentMixin


class ProjectManager(DeploymentMixin, ProjectManagerBase):
    """Coordinates repositories, builds and project service definitions."""


__all__ = ["ProjectError", "ProjectManager"]
