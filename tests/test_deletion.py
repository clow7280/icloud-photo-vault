from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import NOW, make_asset, requires_7zz

from icloud_photo_vault.archive import run_archive, write_password
from icloud_photo_vault.backup import sync_account
from icloud_photo_vault.deletion import (
    CopyChecker,
    auto_delete_plan,
    cutoff_month,
    execute_deletion,
    manual_delete_preview,
    years_before,
)
from icloud_photo_vault.stats import month_stats


def sync(db, cfg, account, library):
    return sync_account(db, cfg.backup_root, cfg.timezone, account, library)


def copies(db, cfg, asset_id):
    with db.connect() as conn:
        asset = conn.execute("SELECT * FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
        return CopyChecker(conn, cfg.backup_root, cfg.archive_root).check(asset)


def state(db, asset_id):
    with db.connect() as conn:
        return conn.execute("SELECT state FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()["state"]


@pytest.fixture
def populated(db, cfg, account, library):
    make_asset(library, "old", "IMG_0001.HEIC", datetime(2022, 3, 1, tzinfo=UTC), {"original": "HEIC", "live": "MOV"})
    make_asset(library, "old2", "IMG_0002.HEIC", datetime(2022, 3, 2, tzinfo=UTC))
    make_asset(library, "recent", "IMG_0003.HEIC", datetime(2025, 8, 1, tzinfo=UTC))
    make_asset(library, "nodate", "IMG_0004.HEIC", None)
    sync(db, cfg, account, library)
    return library


def test_backup_alone_is_one_copy(db, cfg, populated):
    assert copies(db, cfg, "old") == (1, "所在月份还没有归档")
    assert copies(db, cfg, "nodate") == (0, "没有拍摄日期")


@requires_7zz
def test_archive_makes_two_copies(db, cfg, populated):
    write_password(cfg, "correct horse battery")
    run_archive(db, cfg, NOW)
    assert copies(db, cfg, "old") == (2, None)


def test_missing_live_video_means_not_backed_up(db, cfg, populated):
    (cfg.backup_root / "alice/2022/03/IMG_0001.MOV").unlink()
    count, problem = copies(db, cfg, "old")
    assert count == 0 and "IMG_0001.MOV" in problem


def test_years_before_handles_leap_day():
    assert years_before(datetime(2028, 2, 29, tzinfo=UTC), 2) == datetime(2026, 2, 28, tzinfo=UTC)


@requires_7zz
def test_auto_plan_only_old_assets_with_two_copies(db, cfg, account, populated):
    write_password(cfg, "correct horse battery")
    run_archive(db, cfg, NOW)

    plan = auto_delete_plan(db, cfg.backup_root, cfg.archive_root, account["id"], NOW, 2, 500)
    assert [c.asset_id for c in plan] == ["old", "old2"]

    plan = auto_delete_plan(db, cfg.backup_root, cfg.archive_root, account["id"], NOW, 2, 1)
    assert [c.asset_id for c in plan] == ["old"]


def test_auto_plan_is_empty_without_archive(db, cfg, account, populated):
    assert auto_delete_plan(db, cfg.backup_root, cfg.archive_root, account["id"], NOW, 2, 500) == []


@requires_7zz
def test_auto_deletion_deletes_and_logs(db, cfg, account, populated):
    write_password(cfg, "correct horse battery")
    run_archive(db, cfg, NOW)
    plan = auto_delete_plan(db, cfg.backup_root, cfg.archive_root, account["id"], NOW, 2, 500)

    result = execute_deletion(
        db, cfg.backup_root, cfg.archive_root, populated, account["id"], [c.asset_id for c in plan], "auto"
    )

    assert (result.deleted, result.skipped, result.errors) == (2, 0, [])
    assert populated.deleted == {"rec-old", "rec-old2"}
    assert state(db, "old") == "deleted"
    with db.connect() as conn:
        log = conn.execute("SELECT asset_id, mode, result FROM deletions ORDER BY id").fetchall()
    assert [tuple(r) for r in log] == [("old", "auto", "ok"), ("old2", "auto", "ok")]
    # NAS 上的备份保留
    assert (cfg.backup_root / "alice/2022/03/IMG_0001.HEIC").exists()

    # 删除后再同步，状态保持为已删除，不会被当成用户自己删除
    result = sync(db, cfg, account, populated)
    assert result.gone == 0
    assert state(db, "old") == "deleted"


def test_auto_mode_refuses_single_copy(db, cfg, account, populated):
    result = execute_deletion(db, cfg.backup_root, cfg.archive_root, populated, account["id"], ["old"], "auto")
    assert (result.deleted, result.skipped) == (0, 1)
    assert populated.deleted == set()


def test_manual_preview_selects_months(db, cfg, account, populated):
    items = manual_delete_preview(db, cfg.backup_root, cfg.archive_root, account["id"], ["2022-03"])
    assert [c.asset_id for c in items] == ["old", "old2"]
    assert all(c.copies == 1 and c.size == 5000 for c in items)
    items = manual_delete_preview(db, cfg.backup_root, cfg.archive_root, account["id"], ["2025-08", "unknown"])
    assert sorted(c.asset_id for c in items) == ["nodate", "recent"]
    assert [c.backed_up for c in items if c.asset_id == "nodate"] == [False]


def test_cutoff_month_uses_local_time():
    # 2024-11-04 12:00 UTC 是上海时间当天晚上，仍属于 11 月
    assert cutoff_month(NOW, 2, ZoneInfo("Asia/Shanghai")) == "2024-11"
    assert cutoff_month(datetime(2026, 10, 31, 20, tzinfo=UTC), 2, ZoneInfo("Asia/Shanghai")) == "2024-11"


@requires_7zz
def test_month_stats_counts_space(db, cfg, account, populated):
    write_password(cfg, "correct horse battery")
    run_archive(db, cfg, NOW)
    cutoff = years_before(NOW, 2).isoformat(timespec="seconds")
    with db.connect() as conn:
        rows = {r["month"]: r for r in month_stats(conn, account["id"], cutoff)}
    march = rows["2022-03"]
    assert (march["two_copies"], march["auto_ready"], march["auto_bytes"]) == (2, 2, 10000)
    assert march["backed_bytes"] == march["two_copies_bytes"] == march["icloud_bytes"] == 10000
    assert rows["2025-08"]["auto_ready"] == 0
    assert rows["unknown"]["backed_up"] == 1 and rows["unknown"]["auto_ready"] == 0


def test_manual_deletion_needs_backup_only(db, cfg, account, populated):
    result = execute_deletion(db, cfg.backup_root, cfg.archive_root, populated, account["id"], ["recent"], "manual")
    assert result.deleted == 1
    assert populated.deleted == {"rec-recent"}


def test_manual_deletion_skips_files_missing_on_disk(db, cfg, account, populated):
    (cfg.backup_root / "alice/2025/08/IMG_0003.HEIC").unlink()
    result = execute_deletion(db, cfg.backup_root, cfg.archive_root, populated, account["id"], ["recent"], "manual")
    assert (result.deleted, result.skipped) == (0, 1)
    assert populated.deleted == set()


def test_failures_are_logged_and_stop_after_limit(db, cfg, account, library):
    ids = []
    for i in range(7):
        make_asset(library, f"f{i}", f"IMG_{i:04d}.HEIC", datetime(2022, 1, i + 1, tzinfo=UTC))
        library.fail_delete.add(f"rec-f{i}")
        ids.append(f"f{i}")
    sync(db, cfg, account, library)

    result = execute_deletion(db, cfg.backup_root, cfg.archive_root, library, account["id"], ids, "manual")

    assert result.deleted == 0
    assert len(result.errors) == 6 and "停止" in result.errors[-1]
    assert state(db, "f0") == "in_icloud"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM deletions WHERE result = 'error'").fetchone()[0] == 5
