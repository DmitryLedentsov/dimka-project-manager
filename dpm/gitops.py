from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    timestamp: str
    message: str


class GitRepository:
    def __init__(self) -> None:
        self.env = os.environ.copy()
        self.env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
            }
        )

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: int = 120,
    ) -> str:
        try:
            result = subprocess.run(
                args,
                cwd=cwd,
                env=self.env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitError(str(exc)) from exc
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "Git command failed"
            raise GitError(message)
        return result.stdout.strip()

    def clone(self, url: str, branch: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            ["git", "clone", "--single-branch", "--branch", branch, url, str(destination)],
            timeout=300,
        )

    def ensure_checkout(self, url: str, branch: str, destination: Path) -> None:
        if not (destination / ".git").exists():
            if destination.exists():
                for child in destination.iterdir():
                    if child.is_dir():
                        import shutil

                        shutil.rmtree(child)
                    else:
                        child.unlink()
            self.clone(url, branch, destination)
            return
        self._run(["git", "remote", "set-url", "origin", url], cwd=destination)
        self._run(["git", "fetch", "--prune", "origin", branch], cwd=destination, timeout=300)
        self._run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=destination)
        self._run(["git", "reset", "--hard", f"origin/{branch}"], cwd=destination)

    def remote_sha(self, url: str, branch: str) -> str:
        output = self._run(["git", "ls-remote", url, f"refs/heads/{branch}"], timeout=60)
        if not output:
            raise GitError(f"Branch '{branch}' was not found")
        return output.split()[0]

    def current_commit(self, destination: Path) -> CommitInfo:
        output = self._run(
            ["git", "show", "-s", "--format=%H%x1f%cI%x1f%s", "HEAD"],
            cwd=destination,
        )
        sha, timestamp, message = output.split("\x1f", 2)
        return CommitInfo(sha=sha, timestamp=timestamp, message=message)
