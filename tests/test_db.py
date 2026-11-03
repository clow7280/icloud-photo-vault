import sqlite3

from icloud_photo_vault.db import Database


def test_adds_columns_missing_from_old_databases(tmp_path):
    path = tmp_path / "vault.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, apple_id TEXT NOT NULL UNIQUE,
            domain TEXT NOT NULL DEFAULT 'cn', auth_state TEXT NOT NULL DEFAULT 'need_login',
            last_sync_at TEXT, last_sync_error TEXT, created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("INSERT INTO accounts (name, apple_id, created_at) VALUES ('alice', 'a@b.c', 'x')")
    conn.commit()
    conn.close()

    db = Database(path)
    with db.connect() as conn:
        row = conn.execute("SELECT name, icloud_total, icloud_videos, counted_at FROM accounts").fetchone()
    assert tuple(row) == ("alice", None, None, None)
    Database(path)  # 再次打开不会重复添加
