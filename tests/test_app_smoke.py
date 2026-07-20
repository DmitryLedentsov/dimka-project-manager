from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dpm.app import create_app
from dpm.config import Settings


class AppSmokeTest(unittest.TestCase):
    def test_login_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root / "data", root / "logs", root / "config.env", "127.0.0.1", 18787, "/admin", "http://127.0.0.1/admin", "secret", "token", "admin", None, True, 60, 60, False)
            app = create_app(settings, start_background=False)
            app.testing = True
            client = app.test_client()
            self.assertEqual(client.get("/admin/login").status_code, 200)
            self.assertEqual(client.get("/admin/api/health").status_code, 200)
            context = app.extensions["dpm"]
            connection = context.db.connect()
            try:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                connection.close()
            self.assertIn("compose_file", columns)
            self.assertIn("compose_project_name", columns)
            self.assertNotIn("services", tables)
            context.shutdown()


if __name__ == "__main__":
    unittest.main()
