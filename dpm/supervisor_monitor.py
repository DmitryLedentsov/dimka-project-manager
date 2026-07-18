from __future__ import annotations

import time

from .supervisor_base import ServiceError
from .utils import utc_now


class MonitoringMixin:
    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(2):
            services = self.db.fetchall(
                "SELECT * FROM services WHERE status IN ('running','starting','unhealthy')"
            )
            for service in services:
                service_id = int(service["id"])
                pid = service.get("pid")
                process = self._processes.get(service_id)
                exit_code = process.poll() if process else None
                alive = bool(pid and self._pid_alive(int(pid)))
                if alive and exit_code is None:
                    continue

                self._processes.pop(service_id, None)
                self.db.update(
                    "services",
                    service_id,
                    {
                        "pid": None,
                        "status": "failed",
                        "exit_code": exit_code,
                        "stopped_at": utc_now(),
                        "last_error": f"Process exited with code {exit_code}" if exit_code is not None else "Process disappeared",
                        "updated_at": utc_now(),
                    },
                )
                if not service.get("enabled"):
                    continue
                policy = service.get("restart_policy", "always")
                if policy == "never" or (policy == "on-failure" and exit_code == 0):
                    continue
                if not self._allow_restart(service_id):
                    self.db.update(
                        "services",
                        service_id,
                        {
                            "last_error": "Restart limit reached (5 restarts per minute)",
                            "updated_at": utc_now(),
                        },
                    )
                    continue
                time.sleep(1)
                try:
                    self.db.execute(
                        "UPDATE services SET restart_count = restart_count + 1 WHERE id = ?",
                        [service_id],
                    )
                    self.start_service(service_id, automatic=True)
                except ServiceError:
                    pass

    def _allow_restart(self, service_id: int) -> bool:
        now = time.monotonic()
        history = self._restart_history[service_id]
        while history and history[0] < now - 60:
            history.popleft()
        if len(history) >= 5:
            return False
        history.append(now)
        return True
