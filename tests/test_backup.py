import hashlib
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from conftest import make_asset

from icloud_photo_vault.backup import sync_account


def sync(db, cfg, account, library):
    return sync_account(db, cfg.backup_root, cfg.timezone, account, library)


def files_of(db, asset_id):
    with db.connect() as conn:
        return {r["role"]: dict(r) for r in conn.execute("SELECT * FROM files WHERE asset_id = ?", (asset_id,))}


def test_downloads_every_version_with_paired_names(db, cfg, account, library):
    taken = datetime(2023, 5, 12, 6, 30, tzinfo=UTC)
    make_asset(library, "a1", "IMG_0001.HEIC", taken, {"original": "HEIC", "live": "MOV", "adjusted": "JPG"})

    result = sync(db, cfg, account, library)

    assert (result.listed, result.new_assets, result.downloaded) == (1, 1, 3)
    files = files_of(db, "a1")
    assert {role: f["rel_path"] for role, f in files.items()} == {
        "original": "2023/05/IMG_0001.HEIC",
        "live": "2023/05/IMG_0001.MOV",
        "adjusted": "2023/05/IMG_0001-adjusted.JPG",
        "xmp": "2023/05/IMG_0001.HEIC.xmp",
    }
    original = cfg.backup_root / "alice" / "2023/05/IMG_0001.HEIC"
    assert hashlib.sha256(original.read_bytes()).hexdigest() == files["original"]["sha256"]
    assert original.stat().st_mtime == taken.timestamp()
    assert not list((cfg.backup_root / "alice").rglob("*.part"))


def test_second_run_downloads_nothing(db, cfg, account, library):
    make_asset(library, "a1", "IMG_0001.HEIC", datetime(2023, 5, 12, tzinfo=UTC))
    sync(db, cfg, account, library)

    result = sync(db, cfg, account, library)

    assert (result.new_assets, result.downloaded) == (0, 0)


def test_month_uses_local_timezone(db, cfg, account, library):
    # UTC 4 月 30 日 20 点是北京时间 5 月 1 日凌晨
    make_asset(library, "a1", "IMG_0001.HEIC", datetime(2023, 4, 30, 20, 0, tzinfo=UTC))
    sync(db, cfg, account, library)
    assert files_of(db, "a1")["original"]["rel_path"].startswith("2023/05/")


def test_same_name_in_same_month_gets_suffix(db, cfg, account, library):
    taken = datetime(2023, 5, 12, tzinfo=UTC)
    make_asset(library, "a1", "IMG_0001.HEIC", taken)
    make_asset(library, "a2", "IMG_0001.HEIC", taken)

    sync(db, cfg, account, library)

    first = files_of(db, "a1")["original"]["rel_path"]
    second = files_of(db, "a2")["original"]["rel_path"]
    assert first == "2023/05/IMG_0001.HEIC"
    assert second.startswith("2023/05/IMG_0001_") and second.endswith(".HEIC")


def test_undated_asset_goes_to_unknown(db, cfg, account, library):
    make_asset(library, "a1", "IMG_0001.HEIC", None)
    sync(db, cfg, account, library)
    assert files_of(db, "a1")["original"]["rel_path"] == "unknown/IMG_0001.HEIC"


def test_size_mismatch_is_reported_and_not_saved(db, cfg, account, library):
    asset = make_asset(library, "a1", "IMG_0001.HEIC", datetime(2023, 5, 12, tzinfo=UTC))
    asset.versions["original"] = replace(asset.versions["original"], size=99)

    result = sync(db, cfg, account, library)

    assert result.errors and "大小不符" in result.errors[0]
    assert "original" not in files_of(db, "a1")
    assert not (cfg.backup_root / "alice" / "2023/05/IMG_0001.HEIC").exists()


def test_changed_edit_is_downloaded_again(db, cfg, account, library):
    asset = make_asset(
        library, "a1", "IMG_0001.HEIC", datetime(2023, 5, 12, tzinfo=UTC), {"original": "HEIC", "adjusted": "JPG"}
    )
    sync(db, cfg, account, library)
    new_data = b"edited again" * 100
    library.contents[("a1", "adjusted")] = new_data
    asset.versions["adjusted"] = replace(asset.versions["adjusted"], size=len(new_data), checksum="new")

    result = sync(db, cfg, account, library)

    assert result.downloaded == 1
    assert files_of(db, "a1")["adjusted"]["sha256"] == hashlib.sha256(new_data).hexdigest()


def test_missing_file_on_disk_is_downloaded_again(db, cfg, account, library):
    make_asset(library, "a1", "IMG_0001.HEIC", datetime(2023, 5, 12, tzinfo=UTC))
    sync(db, cfg, account, library)
    (cfg.backup_root / "alice" / "2023/05/IMG_0001.HEIC").unlink()

    assert sync(db, cfg, account, library).downloaded == 1


def test_assets_removed_from_icloud_are_marked_gone(db, cfg, account, library):
    make_asset(library, "a1", "IMG_0001.HEIC", datetime(2023, 5, 12, tzinfo=UTC))
    make_asset(library, "a2", "IMG_0002.HEIC", datetime(2023, 5, 13, tzinfo=UTC))
    sync(db, cfg, account, library)
    library.deleted.add("rec-a2")

    result = sync(db, cfg, account, library)

    assert result.gone == 1
    with db.connect() as conn:
        states = dict(conn.execute("SELECT asset_id, state FROM assets").fetchall())
    assert states == {"a1": "in_icloud", "a2": "gone"}
    assert (cfg.backup_root / "alice" / "2023/05/IMG_0002.HEIC").exists()


def test_sync_records_icloud_count_and_reports_progress(db, cfg, account, library):
    for i in range(60):
        name = f"IMG_{i:04d}.MOV" if i % 10 == 0 else f"IMG_{i:04d}.HEIC"
        make_asset(library, f"a{i}", name, datetime(2023, 5, 1, tzinfo=UTC))
    messages = []
    sync_account(
        db, cfg.backup_root, cfg.timezone, account, library, lambda *args, **kw: messages.append((*args, kw.get("note")))
    )
    with db.connect() as conn:
        row = conn.execute("SELECT icloud_total, icloud_videos FROM accounts").fetchone()
    assert (row["icloud_total"], row["icloud_videos"]) == (60, 6)
    assert messages[0] == ("已检查 0 / 60 张", 0, 60, "iCloud 共 60 张，其中视频 6 个")
    assert messages[1] == ("已检查 50 / 60 张", 50, 60, "新下载 50 个文件，其余已在 NAS 上")


def test_sync_continues_when_count_fails(db, cfg, account, library, monkeypatch):
    from icloud_photo_vault.source import SourceError

    def fail():
        raise SourceError("网络错误")

    make_asset(library, "a1", "IMG_0001.HEIC", datetime(2023, 5, 1, tzinfo=UTC))
    monkeypatch.setattr(library, "count", fail)
    assert sync(db, cfg, account, library).listed == 1


def test_interrupted_download_resumes(db, cfg, account, library, monkeypatch):
    monkeypatch.setattr("icloud_photo_vault.backup.RETRY_DELAYS", (0, 0))
    make_asset(library, "a1", "IMG_0001.HEIC", datetime(2023, 5, 12, tzinfo=UTC))
    library.flaky[("a1", "original")] = 2

    result = sync(db, cfg, account, library)

    assert (result.errors, result.downloaded) == ([], 1)
    saved = files_of(db, "a1")["original"]
    data = library.contents[("a1", "original")]
    assert saved["sha256"] == hashlib.sha256(data).hexdigest()
    assert (cfg.backup_root / "alice" / saved["rel_path"]).read_bytes() == data


def test_failure_after_retries_is_summarized(db, cfg, account, library, monkeypatch):
    monkeypatch.setattr("icloud_photo_vault.backup.RETRY_DELAYS", (0, 0))
    for i in range(5):
        make_asset(library, f"a{i}", f"IMG_000{i}.HEIC", datetime(2023, 5, 12, tzinfo=UTC))
        library.flaky[(f"a{i}", "original")] = 3

    result = sync(db, cfg, account, library)

    assert result.failed == 5 and result.errors[0] == "IMG_0000.HEIC：模拟网络中断"
    with db.connect() as conn:
        error = conn.execute("SELECT last_sync_error FROM accounts").fetchone()[0]
    assert error.startswith("5 张照片没有备份成功，下次同步会自动重试：IMG_0000.HEIC：模拟网络中断；")
    assert error.endswith("等")
    # 下一次同步时网络恢复，补齐后不再显示问题
    sync(db, cfg, account, library)
    with db.connect() as conn:
        assert conn.execute("SELECT last_sync_error FROM accounts").fetchone()[0] is None


def test_batches_cover_every_asset(db, cfg, account, library, monkeypatch):
    monkeypatch.setattr("icloud_photo_vault.backup.BATCH_SIZE", 3)
    for i in range(7):
        make_asset(library, f"a{i}", f"IMG_000{i}.HEIC", datetime(2023, 5, 12, tzinfo=UTC))

    first = sync(db, cfg, account, library)
    library.deleted.add("rec-a6")
    second = sync(db, cfg, account, library)

    assert (first.listed, first.new_assets, first.downloaded) == (7, 7, 7)
    assert (second.listed, second.downloaded, second.gone) == (6, 0, 1)


def test_one_connection_per_batch(db, cfg, account, library, monkeypatch):
    for i in range(250):
        make_asset(library, f"a{i}", f"IMG_{i:04d}.HEIC", datetime(2023, 5, 12, tzinfo=UTC))
    sync(db, cfg, account, library)
    opened = []
    connect = db.connect
    monkeypatch.setattr(db, "connect", lambda: (opened.append(1), connect())[1])

    assert sync(db, cfg, account, library).downloaded == 0
    # 查数量、3 批照片、最后标记不在 iCloud 的照片
    assert len(opened) == 5


def test_listing_error_keeps_assets_already_listed(db, cfg, account, library, monkeypatch):
    from icloud_photo_vault.source import SourceError

    for i in range(5):
        make_asset(library, f"a{i}", f"IMG_000{i}.HEIC", datetime(2023, 5, 12, tzinfo=UTC))
    sync(db, cfg, account, library)
    make_asset(library, "new", "IMG_0009.HEIC", datetime(2023, 5, 12, tzinfo=UTC))
    assets = library.assets

    def broken():
        yield from (assets[5], assets[0])
        raise SourceError("列举中断")

    monkeypatch.setattr(library, "iter_assets", broken)
    with pytest.raises(SourceError):
        sync(db, cfg, account, library)

    # 中断前列出的照片已经备份，没列到的照片不会被当成已从 iCloud 删除
    assert "original" in files_of(db, "new")
    with db.connect() as conn:
        states = {r["asset_id"]: r["state"] for r in conn.execute("SELECT asset_id, state FROM assets")}
    assert set(states.values()) == {"in_icloud"} and len(states) == 6


def test_download_does_not_hold_write_lock(db, cfg, account, library):
    make_asset(library, "a1", "IMG_0001.HEIC", datetime(2023, 5, 12, tzinfo=UTC))
    make_asset(library, "a2", "IMG_0002.HEIC", datetime(2023, 5, 12, tzinfo=UTC))
    download = library.download

    def download_and_write(asset, role, start=0):
        # 另一个连接不等待锁，同步占着写锁时这里会报 database is locked
        other = sqlite3.connect(db.path, timeout=0)
        with other:
            other.execute("UPDATE settings SET value = ? WHERE key = 'last_cycle_at'", (asset.asset_id,))
        other.close()
        yield from download(asset, role, start)

    library.download = download_and_write
    result = sync(db, cfg, account, library)

    assert (result.failed, result.downloaded) == (0, 2)
    assert db.get_setting("last_cycle_at") == "a2"
