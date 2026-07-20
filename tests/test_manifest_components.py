from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dpm.manifest import ManifestError, load_manifest


class ComponentManifestTest(unittest.TestCase):
    def load(self, text: str):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dpm.yaml").write_text(text, encoding="utf-8")
            return load_manifest(root)

    def test_process_and_static_components(self) -> None:
        manifest = self.load(
            """
version: 1
project:
  name: example
build:
  - ./build.sh
components:
  api:
    type: process
    command: [java, -jar, app.jar]
    env_file: /etc/example.env
    healthcheck:
      tcp: 127.0.0.1:8080
      timeout: 45s
  web:
    type: static
    depends_on: [api]
    source: frontend/dist
    target: /var/www/example
    url: https://example.test
    healthcheck:
      http: https://example.test
      timeout: 15s
"""
        )
        self.assertEqual(manifest.name, "example")
        self.assertEqual(manifest.build_commands, ["./build.sh"])
        self.assertEqual([item.name for item in manifest.components], ["api", "web"])
        api, web = manifest.components
        self.assertEqual(api.type_name, "process")
        self.assertEqual(api.process_healthcheck["type"], "command")
        self.assertIn("/dev/tcp/127.0.0.1/8080", api.process_healthcheck["command"])
        self.assertEqual(web.type_name, "static")
        self.assertEqual(web.depends_on, ["api"])
        self.assertEqual(web.config["source"], "frontend/dist")

    def test_legacy_services_are_process_components(self) -> None:
        manifest = self.load(
            """
version: 1
project:
  name: legacy
build:
  commands:
    - ./build.sh
services:
  - name: backend
    command: python app.py
    working_directory: backend
    environment:
      PORT: "9000"
    restart: always
"""
        )
        self.assertEqual(len(manifest.components), 1)
        component = manifest.components[0]
        self.assertEqual(component.name, "backend")
        self.assertEqual(component.type_name, "process")
        self.assertEqual(component.cwd, "backend")
        self.assertEqual(component.env["PORT"], "9000")

    def test_unknown_component_type_is_rejected(self) -> None:
        with self.assertRaises(ManifestError):
            self.load(
                """
version: 1
components:
  database:
    type: database
"""
            )

    def test_unknown_dependency_is_rejected(self) -> None:
        with self.assertRaises(ManifestError):
            self.load(
                """
version: 1
components:
  api:
    type: process
    command: python app.py
    depends_on: [missing]
"""
            )


if __name__ == "__main__":
    unittest.main()
