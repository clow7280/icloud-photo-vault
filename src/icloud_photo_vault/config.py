"""运行配置，全部来自环境变量，便于在容器中设置。"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:
    backup_root: Path
    archive_root: Path
    state_dir: Path
    staging_dir: Path
    timezone: ZoneInfo
    sevenzip: str
    source: str
    host: str
    port: int
    socket: Path | None
    base_path: str
    require_admin: bool

    @property
    def db_path(self) -> Path:
        return self.state_dir / "vault.db"

    @property
    def sessions_dir(self) -> Path:
        return self.state_dir / "sessions"

    @property
    def password_file(self) -> Path:
        return self.state_dir / "archive-password"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Config":
        data = Path(env.get("VAULT_DATA_DIR", "data"))
        state_dir = Path(env.get("VAULT_STATE_DIR", data / "state"))
        base_path = env.get("VAULT_BASE_PATH", "").rstrip("/")
        if base_path and not base_path.startswith("/"):
            raise ValueError(f"VAULT_BASE_PATH 必须以 / 开头，实际是 {base_path}")
        source = env.get("VAULT_SOURCE", "icloud")
        if source not in ("icloud", "fake"):
            raise ValueError(f"VAULT_SOURCE 只能是 icloud 或 fake，实际是 {source}")
        return cls(
            backup_root=Path(env.get("VAULT_BACKUP_ROOT", data / "backup")),
            archive_root=Path(env.get("VAULT_ARCHIVE_ROOT", data / "archive")),
            state_dir=state_dir,
            staging_dir=Path(env.get("VAULT_STAGING_DIR", state_dir / "staging")),
            timezone=ZoneInfo(env.get("VAULT_TIMEZONE", "Asia/Shanghai")),
            sevenzip=env.get("VAULT_SEVENZIP", "7zz"),
            source=source,
            host=env.get("VAULT_HOST", "127.0.0.1"),
            port=int(env.get("VAULT_PORT", "8730")),
            socket=Path(env["VAULT_SOCKET"]) if env.get("VAULT_SOCKET") else None,
            base_path=base_path,
            require_admin=env.get("VAULT_REQUIRE_ADMIN", "0") == "1",
        )

    def ensure_dirs(self) -> None:
        for path in (self.backup_root, self.archive_root, self.staging_dir, self.sessions_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
