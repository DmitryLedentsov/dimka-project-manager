from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


class EnvironmentError(RuntimeError):
    pass


class ProjectEnvironment:
    MAX_BYTES = 128 * 1024

    def __init__(self, root: Path = Path('/etc')) -> None:
        self.root = root

    def path(self, project_name: str) -> Path:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', project_name) or '..' in project_name:
            raise EnvironmentError('Invalid project name for environment file')
        return self.root / project_name / 'compose.env'

    def read(self, project_name: str) -> dict[str, object]:
        path = self.path(project_name)
        if not path.exists():
            return {'path': str(path), 'exists': False, 'content': ''}
        if not path.is_file():
            raise EnvironmentError(f'Environment path is not a file: {path}')
        if path.stat().st_size > self.MAX_BYTES:
            raise EnvironmentError('Environment file is too large to edit in DPM')
        return {'path': str(path), 'exists': True, 'content': path.read_text(encoding='utf-8')}

    def write(self, project_name: str, content: str) -> dict[str, object]:
        if not isinstance(content, str):
            raise EnvironmentError('Environment content must be text')
        if '\x00' in content:
            raise EnvironmentError('Environment content contains a NUL byte')
        encoded = content.encode('utf-8')
        if len(encoded) > self.MAX_BYTES:
            raise EnvironmentError('Environment file is too large')

        path = self.path(project_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w',
                encoding='utf-8',
                dir=path.parent,
                prefix='.compose.env.',
                delete=False,
            ) as handle:
                temporary = handle.name
                os.chmod(temporary, 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
            os.chmod(path, 0o600)
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        return self.read(project_name)

    def delete(self, project_name: str) -> None:
        path = self.path(project_name)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        try:
            path.parent.rmdir()
        except OSError:
            pass
