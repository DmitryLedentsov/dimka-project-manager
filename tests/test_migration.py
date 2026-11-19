from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pyzipper

from dpm.migration import MigrationManager


class FakeDatabase:
    def __init__(self, project: dict[str, object]) -> None:
        self.project = project

    def fetchall(self, sql: str, params=()):
        if "SELECT * FROM projects" in sql:
            return [dict(self.project)]
        return []


class FakeProjects:
    def is_busy(self, project_id: int) -> bool:
        return False


class FakeCompose:
    def __init__(self, env_path: Path) -> None:
        self._env_path = env_path

    def env_path(self, project):
        return self._env_path

    def config(self, project):
        return {"services": {}}


class MigrationExportTest(unittest.TestCase):
    def test_export_contains_resolved_project_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            env_file = repository / ".env"
            env_file.write_text("SECRET=value\n", encoding="utf-8")
            project = {
                "id": 1,
                "name": "demo",
                "repository_url": "git@github.com:example/demo.git",
                "branch": "master",
                "repo_path": str(repository),
                "compose_file": "compose.yml",
                "compose_project_name": "demo",
                "env_file": None,
                "auto_update": 1,
                "poll_interval": 60,
                "desired_state": "running",
                "deployed_commit": "abc",
            }
            settings = SimpleNamespace(
                data_dir=root / "data",
                poll_interval=60,
            )
            manager = MigrationManager(
                settings,
                FakeDatabase(project),
                FakeProjects(),
                FakeCompose(env_file),
            )
            destination = root / "migration.zip"
            manager.export_archive(
                destination,
                "password123",
                include_git_credentials=False,
                include_volumes=False,
            )

            with pyzipper.AESZipFile(destination, "r") as archive:
                archive.setpassword(b"password123")
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(manifest["format"], "dpm-migration")
                self.assertEqual(manifest["projects"][0]["name"], "demo")
                self.assertEqual(len(manifest["files"]), 1)
                entry = manifest["files"][0]
                self.assertEqual(entry["location"], "repository")
                self.assertEqual(entry["destination"], ".env")
                self.assertEqual(archive.read(entry["archive_path"]), b"SECRET=value\n")


if __name__ == "__main__":
    unittest.main()
