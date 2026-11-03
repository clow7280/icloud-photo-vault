import hashlib
from dataclasses import replace
from datetime import UTC, datetime

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
