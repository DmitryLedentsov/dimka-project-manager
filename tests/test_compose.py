from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dpm.compose import ComposeError, ComposeRuntime, _json_items
from dpm.config import Settings


class ComposeRuntimeTest(unittest.TestCase):
    def settings(self, root: Path) -> Settings:
        return Settings(root, root, root / "config.env", "127.0.0.1", 8787, "/admin", "http://localhost/admin", "secret", "token", "admin", None, True, 60, 60, False)

    def test_json_stream_accepts_array_and_lines(self) -> None:
        self.assertEqual(len(_json_items('[{"Service":"api"}]')), 1)
        self.assertEqual(len(_json_items('{"Service":"api"}\n{"Service":"web"}')), 2)

    def test_compose_file_must_stay_inside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            repository.mkdir()
            runtime = ComposeRuntime(self.settings(root))
            project = {"repo_path": str(repository), "compose_file": "../compose.yml", "name": "app", "compose_project_name": "app", "env_file": None}
            with self.assertRaises(ComposeError):
                runtime.compose_path(project)

    def test_restart_policy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            repository.mkdir()
            (repository / "compose.yml").write_text("services: {}", encoding="utf-8")
            runtime = ComposeRuntime(self.settings(root))
            project = {"repo_path": str(repository), "compose_file": "compose.yml", "name": "app", "compose_project_name": "app", "env_file": None}
            with patch.object(runtime, "config", return_value={"services": {"api": {"restart": "always"}}}):
                with self.assertRaises(ComposeError):
                    runtime.validate(project)

    def test_services_are_derived_from_compose_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            repository.mkdir()
            (repository / "compose.yml").write_text("services: {}", encoding="utf-8")
            runtime = ComposeRuntime(self.settings(root))
            project = {"repo_path": str(repository), "compose_file": "compose.yml", "name": "app", "compose_project_name": "app", "env_file": None, "deployed_commit": "abc"}
            config = {"services": {"api": {"image": "example/api", "labels": {"dpm.role": "backend"}}}}
            with patch.object(runtime, "config", return_value=config), patch.object(runtime, "_ps", return_value=[]):
                services = runtime.services(project)
            self.assertEqual(services[0]["name"], "api")
            self.assertEqual(services[0]["role"], "backend")
            self.assertEqual(services[0]["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
