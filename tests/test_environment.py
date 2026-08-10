import stat
import tempfile
import unittest
from pathlib import Path

from dpm.environment import EnvironmentError, ProjectEnvironment


class ProjectEnvironmentTests(unittest.TestCase):
    def test_write_read_replace_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectEnvironment(Path(directory))
            first = store.write('asteroids-game', 'TOKEN=one\n')
            self.assertTrue(first['exists'])
            self.assertEqual(first['content'], 'TOKEN=one\n')
            path = Path(str(first['path']))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            second = store.write('asteroids-game', 'TOKEN=two\nPORT=8080\n')
            self.assertEqual(second['content'], 'TOKEN=two\nPORT=8080\n')
            self.assertEqual(store.read('asteroids-game')['content'], second['content'])

            store.delete('asteroids-game')
            self.assertFalse(store.read('asteroids-game')['exists'])

    def test_rejects_unsafe_project_name_and_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectEnvironment(Path(directory))
            with self.assertRaises(EnvironmentError):
                store.path('../root')
            with self.assertRaises(EnvironmentError):
                store.write('safe-project', 'BAD=hello\x00world')


if __name__ == '__main__':
    unittest.main()
