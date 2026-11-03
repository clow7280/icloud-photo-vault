"""SQLite 状态库。"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    apple_id TEXT NOT NULL UNIQUE,
    domain TEXT NOT NULL DEFAULT 'cn',
    auth_state TEXT NOT NULL DEFAULT 'need_login',
    last_sync_at TEXT,
    last_sync_error TEXT,
    created_at TEXT NOT NULL
);

-- state: in_icloud 仍在 iCloud；deleted 已由本工具删除；gone 在 iCloud 中被用户自己删除
-- roles: 这张照片应有的文件，逗号分隔，例如 original,live,xmp
CREATE TABLE IF NOT EXISTS assets (
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    asset_id TEXT NOT NULL,
    record_name TEXT NOT NULL,
    change_tag TEXT,
    filename TEXT NOT NULL,
    item_type TEXT NOT NULL,
    taken_at TEXT,
    month TEXT NOT NULL,
    size INTEGER NOT NULL,
    roles TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'in_icloud',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    deleted_at TEXT,
    PRIMARY KEY (account_id, asset_id)
);
CREATE INDEX IF NOT EXISTS assets_month ON assets (account_id, month);

-- role: original、adjusted、live、xmp；rel_path 相对账号的备份目录
CREATE TABLE IF NOT EXISTS files (
    account_id INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    role TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    expected_size INTEGER,
    remote_checksum TEXT,
    size INTEGER,
    sha256 TEXT,
    downloaded_at TEXT,
    PRIMARY KEY (account_id, asset_id, role),
    UNIQUE (account_id, rel_path)
);

CREATE TABLE IF NOT EXISTS archives (
    account_id INTEGER NOT NULL,
    month TEXT NOT NULL,
    version INTEGER NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    volumes TEXT NOT NULL,
    file_count INTEGER NOT NULL,
    total_size INTEGER NOT NULL,
    verified_at TEXT NOT NULL,
    PRIMARY KEY (account_id, month)
);

CREATE TABLE IF NOT EXISTS archive_members (
    account_id INTEGER NOT NULL,
    month TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (account_id, month, rel_path)
);

-- mode: auto 或 manual；result: ok 或 error
CREATE TABLE IF NOT EXISTS deletions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    taken_at TEXT,
    mode TEXT NOT NULL,
    result TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);

-- 用户在界面上做的重要操作；detail 里写账号当时的名称，改名后仍能看懂
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    actor TEXT,
    account_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# auto_delete_mode: off 关闭；report 只生成报告；on 自动删除
DEFAULT_SETTINGS = {
    "auto_delete_mode": "report",
    "auto_delete_years": "2",
    "auto_delete_max_per_run": "500",
    "sync_interval_hours": "24",
    "archive_volume_mb": "3900",
    "last_cycle_at": "",
}


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.executemany(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                DEFAULT_SETTINGS.items(),
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_setting(self, key: str) -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else DEFAULT_SETTINGS[key]

    def get_int(self, key: str) -> int:
        return int(self.get_setting(key))

    def add_event(self, actor: str | None, action: str, detail: str, account_id: int | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO events (created_at, actor, account_id, action, detail) VALUES (?, ?, ?, ?, ?)",
                (utcnow(), actor, account_id, action, detail),
            )

    def set_setting(self, key: str, value: str) -> None:
        if key not in DEFAULT_SETTINGS:
            raise KeyError(key)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
