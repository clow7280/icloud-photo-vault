import json
from datetime import UTC, datetime

import pytest
from conftest import NOW, make_asset, requires_7zz

from icloud_photo_vault.archive import (
    ArchiveError,
    ManifestEntry,
    SevenZip,
    read_password,
    run_archive,
    verify_archive,
    write_password,
)
from icloud_photo_vault.backup import sync_account

pytestmark = requires_7zz


@pytest.fixture
def synced(db, cfg, account, library):
    make_asset(library, "a1", "IMG_0001.HEIC", datetime(2023, 5, 12, tzinfo=UTC), {"original": "HEIC", "live": "MOV"})
    make_asset(library, "a2", "中文 名字.JPG", datetime(2023, 5, 20, tzinfo=UTC))
    make_asset(library, "a3", "IMG_0003.HEIC", datetime(2023, 6, 1, tzinfo=UTC))
    make_asset(library, "a4", "IMG_0004.HEIC", datetime(2026, 11, 2, tzinfo=UTC))  # 当月，不归档
    sync_account(db, cfg.backup_root, cfg.timezone, account, library)
    write_password(cfg, "correct horse battery")
    return library


def archives(db):
    with db.connect() as conn:
        return {r["month"]: dict(r) for r in conn.execute("SELECT * FROM archives")}


def test_archives_closed_months_and_verifies(db, cfg, synced):
    result = run_archive(db, cfg, NOW)

    assert (result.built, result.errors) == (2, [])
    rows = archives(db)
    assert set(rows) == {"2023-05", "2023-06"}
    volumes = json.loads(rows["2023-05"]["volumes"])
    assert volumes == ["alice/2023/alice-2023-05-v1.7z.001"]
    assert (cfg.archive_root / volumes[0]).is_file()
    assert rows["2023-05"]["file_count"] == 5  # 两张照片的原图、Live 视频和 XMP
    assert not list(cfg.staging_dir.iterdir())


def test_archive_content_is_encrypted_and_complete(db, cfg, synced):
    run_archive(db, cfg, NOW)
    first = cfg.archive_root / "alice/2023/alice-2023-05-v1.7z.001"
    sz = SevenZip(cfg.sevenzip)

    listed = dict(sz.list_files(first, "correct horse battery"))
    assert "2023/05/中文 名字.JPG" in listed
    with pytest.raises(ArchiveError):
        sz.list_files(first, "wrong password")


def test_second_run_keeps_unchanged_months(db, cfg, synced):
    run_archive(db, cfg, NOW)
    result = run_archive(db, cfg, NOW)
    assert (result.built, result.unchanged) == (0, 2)


def test_changed_month_is_repacked_and_old_version_removed(db, cfg, account, synced):
    run_archive(db, cfg, NOW)
    old = cfg.archive_root / "alice/2023/alice-2023-06-v1.7z.001"
    synced.assets[2].xmp = b"<xmp favorite='1'/>"
    sync_account(db, cfg.backup_root, cfg.timezone, account, synced)

    result = run_archive(db, cfg, NOW)

    assert result.built == 1
    assert archives(db)["2023-06"]["version"] == 2
    assert not old.exists()
    assert (cfg.archive_root / "alice/2023/alice-2023-06-v2.7z.001").exists()


def test_missing_volume_triggers_rebuild(db, cfg, synced):
    run_archive(db, cfg, NOW)
    (cfg.archive_root / "alice/2023/alice-2023-06-v1.7z.001").unlink()

    assert run_archive(db, cfg, NOW).built == 1


def test_split_volumes(db, cfg, account, synced):
    make_asset(synced, "big", "IMG_0100.MOV", datetime(2023, 7, 1, tzinfo=UTC), size=1_500_000)
    sync_account(db, cfg.backup_root, cfg.timezone, account, synced)
    db.set_setting("archive_volume_mb", "1")

    assert run_archive(db, cfg, NOW).errors == []
    volumes = json.loads(archives(db)["2023-07"]["volumes"])
    assert volumes == ["alice/2023/alice-2023-07-v1.7z.001", "alice/2023/alice-2023-07-v1.7z.002"]


def test_verify_rejects_wrong_hash(db, cfg, synced):
    run_archive(db, cfg, NOW)
    first = cfg.archive_root / "alice/2023/alice-2023-06-v1.7z.001"
    sz = SevenZip(cfg.sevenzip)
    listed = sz.list_files(first, "correct horse battery")
    wrong = [ManifestEntry(path, size, "0" * 64) for path, size in listed]
    with pytest.raises(ArchiveError, match="SHA-256"):
        verify_archive(sz, first, "correct horse battery", wrong)


def test_no_password_skips_archive(db, cfg, account, library):
    make_asset(library, "a1", "IMG_0001.HEIC", datetime(2023, 5, 12, tzinfo=UTC))
    sync_account(db, cfg.backup_root, cfg.timezone, account, library)
    result = run_archive(db, cfg, NOW)
    assert result.built == 0 and "归档密码" in result.errors[0]


def test_password_can_only_be_set_once(cfg):
    write_password(cfg, "correct horse battery")
    with pytest.raises(ArchiveError):
        write_password(cfg, "another long password")
    assert read_password(cfg) == "correct horse battery"
    assert oct(cfg.password_file.stat().st_mode & 0o777) == "0o600"
