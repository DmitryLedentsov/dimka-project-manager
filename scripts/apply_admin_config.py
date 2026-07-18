#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dpm.config import load_settings  # noqa: E402
from dpm.db import Database  # noqa: E402
from dpm.security import hash_password  # noqa: E402
from dpm.utils import utc_now  # noqa: E402


def main() -> None:
    settings = load_settings()
    database = Database(settings.database_path)
    now = utc_now()
    password_hash = settings.admin_password_hash or hash_password("admin")
    exact = database.fetchone("SELECT * FROM users WHERE username = ?", [settings.admin_username])
    if exact:
        database.update(
            "users",
            exact["id"],
            {"password_hash": password_hash, "is_default": 1 if settings.admin_is_default else 0, "updated_at": now},
        )
        return
    first = database.fetchone("SELECT * FROM users ORDER BY id LIMIT 1")
    if first:
        database.update(
            "users",
            first["id"],
            {
                "username": settings.admin_username,
                "password_hash": password_hash,
                "is_default": 1 if settings.admin_is_default else 0,
                "updated_at": now,
            },
        )
        return
    database.execute(
        """
        INSERT INTO users (username, password_hash, is_default, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [settings.admin_username, password_hash, 1 if settings.admin_is_default else 0, now, now],
    )


if __name__ == "__main__":
    main()
