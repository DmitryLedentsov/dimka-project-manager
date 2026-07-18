from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"\.git$", "", value)
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-.")
    if not value:
        raise ValueError("Could not derive a safe project name")
    return value[:80]


def repository_name(url: str) -> str:
    cleaned = url.rstrip("/")
    return slugify(cleaned.rsplit("/", 1)[-1].rsplit(":", 1)[-1])


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def tail_file(path: Path, lines: int = 200, max_bytes: int = 512_000) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        data = handle.read()
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-max(1, min(lines, 2000)):])
