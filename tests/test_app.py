import tempfile
import unittest
from pathlib import Path

from dpm.app import create_app
from dpm.config import Settings


class AppTests(unittest.TestCase):
    def test_login_and_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(
                data_dir=root / "data",
                log_dir=root / "logs",
                config_file=root / "config.env",
                host="127.0.0.1",
                port=8787,
                base_path="/admin",
                public_url="http://127.0.0.1:8787/admin",
                secret_key="test-secret",
                cli_token="test-token",
                admin_username="admin",
                admin_password_hash=None,
                admin_is_default=False,
                poll_interval=60,
                debug=False,
            )
            app = create_app(settings, start_background=False)
            app.testing = True
            client = app.test_client()
            response = client.get("/admin/")
            self.assertEqual(response.status_code, 302)
            response = client.post(
                "/admin/login",
                data={"username": "admin", "password": "admin"},
                follow_redirects=True,
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Services overview", response.data)


if __name__ == "__main__":
    unittest.main()
