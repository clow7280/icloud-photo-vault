import json
import os
from datetime import UTC, datetime

import pytest
from conftest import NOW, make_asset, requires_7zz

from icloud_photo_vault import accounts
from icloud_photo_vault.accounts import RenameError, rename_account
from icloud_photo_vault.archive import run_archive, write_password
from icloud_photo_vault.backup import sync_account
from icloud_photo_vault.deletion import auto_delete_plan

pytestmark = requires_7zz


@pytest.fixture
def archived(db, cfg, account, library):
    make_asset(library, "a1", "IMG_0001.HEIC", datetime(2023, 5, 12, tzinfo=UTC), {"original": "HEIC", "live": "MOV"})
    make_asset(library, "a2", "IMG_0002.HEIC", datetime(2023, 6, 1, tzinfo=UTC))
    sync_account(db, cfg.backup_root, cfg.timezone, account, library)
    write_password(cfg, "correct horse battery")
    assert run_archive(db, cfg, NOW).built == 2
    return library


def volumes(db):
    with db.connect() as conn:
        return [v for r in conn.execute("SELECT volumes FROM archives ORDER BY month") for v in json.loads(r[0])]


def test_rename_moves_backup_and_archives(db, cfg, account, archived):
    assert "2 个月的归档包" in rename_account(db, cfg, account["id"], "小明")
    assert not (cfg.backup_root / "alice").exists()
    assert not (cfg.archive_root / "alice").exists()
    assert (cfg.backup_root / "小明").is_dir()
    for name in volumes(db):
        assert name.startswith("小明/2023/小明-2023-")
        assert (cfg.archive_root / name).is_file()
    # 改名后两份副本仍然成立，归档也不需要重做
    assert len(auto_delete_plan(db, cfg.backup_root, cfg.archive_root, account["id"], NOW, 2, 500)) == 2
    assert run_archive(db, cfg, NOW).unchanged == 2


def test_rename_refuses_existing_directory(db, cfg, account, archived):
    (cfg.archive_root / "bob").mkdir()
    before = volumes(db)
    with pytest.raises(RenameError, match="已经存在"):
        rename_account(db, cfg, account["id"], "bob")
    assert volumes(db) == before
    assert (cfg.backup_root / "alice").is_dir()


def test_rename_rolls_back_on_failure(db, cfg, account, archived, monkeypatch):
    real_rename = os.rename

    def failing(src, dst):
        if str(dst) == str(cfg.backup_root / "bob"):
            raise OSError("模拟失败")
        real_rename(src, dst)

    monkeypatch.setattr(accounts.os, "rename", failing)
    before = volumes(db)
    with pytest.raises(OSError):
        rename_account(db, cfg, account["id"], "bob")
    assert volumes(db) == before
    for name in before:
        assert (cfg.archive_root / name).is_file()
    assert not (cfg.archive_root / "bob").exists()
    with db.connect() as conn:
        assert conn.execute("SELECT name FROM accounts").fetchone()[0] == "alice"
