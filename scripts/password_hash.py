#!/usr/bin/env python3
from __future__ import annotations

import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dpm.security import hash_password  # noqa: E402


def main() -> None:
    password = sys.argv[1] if len(sys.argv) > 1 else getpass.getpass("Password: ")
    print(hash_password(password))


if __name__ == "__main__":
    main()
