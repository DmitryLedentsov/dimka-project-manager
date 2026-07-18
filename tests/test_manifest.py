import tempfile
import unittest
from pathlib import Path

from dpm.manifest import ManifestError, load_manifest


class ManifestTests(unittest.TestCase):
    def test_loads_multiple_services(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dpm.yaml").write_text(
                """
version: 1
build:
  commands: ["echo build"]
services:
  - name: api
    command: ["python3", "api.py"]
  - name: worker
    command: "python3 worker.py"
    depends_on: [api]
""",
                encoding="utf-8",
            )
            manifest = load_manifest(root)
            self.assertEqual([service.name for service in manifest.services], ["api", "worker"])
            self.assertEqual(manifest.build_commands, ["echo build"])

    def test_requires_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ManifestError):
                load_manifest(Path(directory))


if __name__ == "__main__":
    unittest.main()
