from __future__ import annotations

import threading
from datetime import datetime, timezone

from .db import Database
from .projects import ProjectManager


class UpdateScheduler:
    def __init__(self, db: Database, projects: ProjectManager) -> None:
        self.db = db
        self.projects = projects
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="dpm-update-scheduler",
            daemon=True,
        )
        self._thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop_event.wait(5):
            now = datetime.now(timezone.utc)
            projects = self.db.fetchall(
                "SELECT id, poll_interval, last_checked_at FROM projects WHERE auto_update = 1"
            )
            for project in projects:
                if self.projects.is_busy(int(project["id"])):
                    continue
                last_checked = project.get("last_checked_at")
                due = True
                if last_checked:
                    try:
                        last = datetime.fromisoformat(last_checked)
                        due = (now - last).total_seconds() >= int(project["poll_interval"])
                    except (ValueError, TypeError):
                        due = True
                if due:
                    self.projects.schedule_check(int(project["id"]))
