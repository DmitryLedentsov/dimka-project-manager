from __future__ import annotations

import threading
from datetime import datetime, timezone

from .db import Database
from .projects import ProjectManager


class UpdateScheduler:
    def __init__(self, db: Database, projects: ProjectManager) -> None:
        self.db = db
        self.projects = projects
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="dpm-git-poller", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop.wait(5):
            now = datetime.now(timezone.utc)
            for project in self.db.fetchall("SELECT * FROM projects WHERE auto_update = 1"):
                last = project.get("last_checked_at")
                if last:
                    try:
                        elapsed = (now - datetime.fromisoformat(last)).total_seconds()
                    except ValueError:
                        elapsed = 10**9
                    if elapsed < int(project.get("poll_interval") or 60):
                        continue
                self.projects.schedule_check(int(project["id"]))
