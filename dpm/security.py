from __future__ import annotations

import hashlib
import hmac
import secrets

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 240_000


def hash_password(password: str, *, iterations: int = _ITERATIONS) -> str:
    if not password:
        raise ValueError("Password must not be empty")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("ascii"),
        iterations,
    ).hex()
    return f"{_ALGORITHM}${iterations}${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_raw, salt, expected = encoded.split("$", 3)
        if algorithm != _ALGORITHM:
            return False
        iterations = int(iterations_raw)
    except (ValueError, TypeError):
        return False

    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("ascii"),
        iterations,
    ).hex()
    return hmac.compare_digest(actual, expected)
