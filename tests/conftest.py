import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from icloud_photo_vault.config import Config
from icloud_photo_vault.db import Database, utcnow
from icloud_photo_vault.source import FakeLibrary, RemoteAsset, RemoteVersion, _fake_bytes

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)

requires_7zz = pytest.mark.skipif(shutil.which("7zz") is None, reason="需要 7-Zip 的 7zz 命令")


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    cfg = Config.from_env({"VAULT_DATA_DIR": str(tmp_path / "data"), "VAULT_SOURCE": "fake"})
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def db(cfg: Config) -> Database:
    return Database(cfg.db_path)


@pytest.fixture
def account(db: Database):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO accounts (name, apple_id, domain, auth_state, created_at) VALUES (?, ?, 'cn', 'ok', ?)",
            ("alice", "alice@example.com", utcnow()),
        )
        return conn.execute("SELECT * FROM accounts WHERE name = 'alice'").fetchone()


def make_asset(
    library: FakeLibrary,
    asset_id: str,
    filename: str,
    taken_at: datetime | None,
    roles: dict[str, str] | None = None,
    size: int = 5000,
    xmp: bytes | None = b"<xmp/>",
) -> RemoteAsset:
    roles = roles or {"original": Path(filename).suffix.lstrip(".")}
    versions = {}
    for role, ext in roles.items():
        data = _fake_bytes(f"{asset_id}:{role}", size)
        library.contents[(asset_id, role)] = data
        versions[role] = RemoteVersion(f"fake://{asset_id}/{role}", size, hashlib.sha1(data).hexdigest(), ext)
    asset = RemoteAsset(
        asset_id=asset_id,
        record_name=f"rec-{asset_id}",
        change_tag="t1",
        filename=filename,
        item_type="movie" if filename.upper().endswith(".MOV") else "image",
        taken_at=taken_at,
        versions=versions,
        xmp=xmp,
    )
    library.assets.append(asset)
    return asset


@pytest.fixture
def library() -> FakeLibrary:
    return FakeLibrary(assets=[], contents={})
