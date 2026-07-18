from __future__ import annotations

from .supervisor_base import ServiceError, SupervisorBase
from .supervisor_lifecycle import LifecycleMixin
from .supervisor_monitor import MonitoringMixin


class ServiceSupervisor(LifecycleMixin, MonitoringMixin, SupervisorBase):
    """Supervises project processes and exposes their lifecycle state."""


__all__ = ["ServiceError", "ServiceSupervisor"]
