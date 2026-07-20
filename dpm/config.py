from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    log_dir: Path
    config_file: Path
    host: str
    port: int
    base_path: str
    public_url: str
    secret_key: str
    cli_token: str
    admin_username: str
    admin_password_hash: str | None
    admin_is_default: bool
    poll_interval: int
    compose_timeout: int
    debug: bool

    @property
    def database_path(self) -> Path:
        return self.data_dir / "dpm.sqlite3"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def secure_cookie(self) -> bool:
        return urlparse(self.public_url).scheme == "https"


def load_settings() -> Settings:
    config_file = Path(os.environ.get("DPM_CONFIG_FILE", "/etc/dpm/config.env"))
    _load_env_file(config_file)
    base_path = os.environ.get("DPM_BASE_PATH", "/admin").strip() or "/admin"
    if not base_path.startswith("/"):
        base_path = "/" + base_path
    base_path = base_path.rstrip("/") or "/admin"
    port = int(os.environ.get("DPM_PORT", "8787"))
    public_url = os.environ.get(
        "DPM_PUBLIC_URL", f"http://127.0.0.1:{port}{base_path}"
    ).rstrip("/")
    return Settings(
        data_dir=Path(os.environ.get("DPM_DATA_DIR", "/var/lib/dpm")),
        log_dir=Path(os.environ.get("DPM_LOG_DIR", "/var/log/dpm")),
        config_file=config_file,
        host=os.environ.get("DPM_HOST", "127.0.0.1"),
        port=port,
        base_path=base_path,
        public_url=public_url,
        secret_key=os.environ.get("DPM_SECRET_KEY", secrets.token_urlsafe(48)),
        cli_token=os.environ.get("DPM_CLI_TOKEN", secrets.token_urlsafe(32)),
        admin_username=os.environ.get("DPM_ADMIN_USERNAME", "admin"),
        admin_password_hash=os.environ.get("DPM_ADMIN_PASSWORD_HASH"),
        admin_is_default=os.environ.get("DPM_ADMIN_IS_DEFAULT", "0") == "1",
        poll_interval=max(15, int(os.environ.get("DPM_POLL_INTERVAL", "60"))),
        compose_timeout=max(30, int(os.environ.get("DPM_COMPOSE_TIMEOUT", "240"))),
        debug=os.environ.get("DPM_DEBUG", "0") == "1",
    )
