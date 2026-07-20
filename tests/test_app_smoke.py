from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dpm.app import create_app
from dpm.config import Settings


class DpmApplicationSmokeTest(unittest.TestCase):
    def test_application_starts_and_migrates_component_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(
                data_dir=root / "data",
                log_dir=root / "logs",
                config_file=root / "dpm.env",
                host="127.0.0.1",
                port=18787,
                base_path="/admin",
                public_url="http://127.0.0.1:18787/admin",
                secret_key="test-secret-key",
                cli_token="test-cli-token",
                admin_username="admin",
                admin_password_hash=None,
                admin_is_default=True,
                poll_interval=60,
                debug=False,
            )
            app = create_app(settings, start_background=False)
            app.testing = True
            client = app.test_client()

            response = client.get("/admin/login")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"DPM", response.data)

            context = app.extensions["dpm"]
            project_columns = {
                row[1]
                for row in context.db.connect().execute("PRAGMA table_info(projects)")
            }
            component_columns = {
                row[1]
                for row in context.db.connect().execute("PRAGMA table_info(services)")
            }
            self.assertIn("desired_state", project_columns)
            self.assertIn("component_type", component_columns)
            self.assertIn("config_json", component_columns)
            self.assertIn("runtime_json", component_columns)
            context.shutdown()


if __name__ == "__main__":
    unittest.main()
