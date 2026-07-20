from __future__ import annotations

from .utils import utc_now


class MonitoringMixin:
    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(2):
            services = self.db.fetchall(
                """
                SELECT * FROM services
                 WHERE component_type = 'process'
                   AND status IN ('running','starting','unhealthy')
                """
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
                        "last_error": (
                            f"Process exited with code {exit_code}"
                            if exit_code is not None
                            else "Process disappeared"
                        ),
                        "updated_at": utc_now(),
                    },
                )

                # A crashed process remains FAILED until the operator explicitly
                # starts/restarts it or a later successful deployment starts it.
