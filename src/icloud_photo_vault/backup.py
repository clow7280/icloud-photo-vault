"""把一个账号的 iCloud 照片增量备份到 NAS。"""

import hashlib
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .db import Database, utcnow
from .progress import Progress, no_progress
from .source import PhotoSource, RemoteAsset, RemoteVersion, SourceError

log = logging.getLogger(__name__)

UNKNOWN_MONTH = "unknown"
MAX_ERRORS_KEPT = 50


@dataclass
class SyncResult:
    listed: int = 0
    new_assets: int = 0
    downloaded: int = 0
    downloaded_bytes: int = 0
    gone: int = 0
    errors: list[str] = field(default_factory=list)

    def add_error(self, message: str) -> None:
        log.warning(message)
        if len(self.errors) < MAX_ERRORS_KEPT:
            self.errors.append(message)


def month_key(taken_at: datetime | None, tz: ZoneInfo) -> str:
    if taken_at is None:
        return UNKNOWN_MONTH
    local = taken_at.astimezone(tz)
    return f"{local.year:04d}-{local.month:02d}"


def month_dir(month: str) -> str:
    return UNKNOWN_MONTH if month == UNKNOWN_MONTH else month.replace("-", "/")


def short_id(asset_id: str) -> str:
    return hashlib.sha1(asset_id.encode()).hexdigest()[:8]


def file_names(stem: str, asset: RemoteAsset) -> dict[str, str]:
    original = asset.versions["original"]
    original_name = f"{stem}.{original.ext}"
    names = {"original": original_name}
    for role, version in asset.versions.items():
        if role == "live":
            names[role] = f"{stem}.{version.ext}"
        elif role == "adjusted":
            names[role] = f"{stem}-adjusted.{version.ext}"
        elif role == "alternative":
            same_ext = version.ext.upper() == original.ext.upper()
            names[role] = f"{stem}-alternative.{version.ext}" if same_ext else f"{stem}.{version.ext}"
    if asset.xmp is not None:
        names["xmp"] = f"{original_name}.xmp"
    return names


def expected_roles(asset: RemoteAsset) -> list[str]:
    roles = list(asset.versions)
    if asset.xmp is not None:
        roles.append("xmp")
    return roles


def _path_taken(conn: sqlite3.Connection, account_id: int, account_dir: Path, asset_id: str, rel: str) -> bool:
    row = conn.execute(
        "SELECT asset_id FROM files WHERE account_id = ? AND rel_path = ?", (account_id, rel)
    ).fetchone()
    if row is not None:
        return row["asset_id"] != asset_id
    return (account_dir / rel).exists()


def assign_paths(
    conn: sqlite3.Connection, account_id: int, account_dir: Path, asset: RemoteAsset, month: str
) -> dict[str, str]:
    """给照片的每个文件分配相对账号目录的路径，已分配的路径保持不变。"""
    existing = {
        row["role"]: row["rel_path"]
        for row in conn.execute(
            "SELECT role, rel_path FROM files WHERE account_id = ? AND asset_id = ?",
            (account_id, asset.asset_id),
        )
    }
    if "original" in existing:
        original = Path(existing["original"])
        directory, stem = original.parent.as_posix(), original.stem
    else:
        directory, stem = month_dir(month), Path(asset.filename).stem
        candidates = file_names(stem, asset).values()
        if any(_path_taken(conn, account_id, account_dir, asset.asset_id, f"{directory}/{n}") for n in candidates):
            stem = f"{stem}_{short_id(asset.asset_id)}"
    return {
        role: existing.get(role, f"{directory}/{name}") for role, name in file_names(stem, asset).items()
    }


def _part_path(dest: Path) -> Path:
    return dest.with_name(f".{dest.name}.part")


def _finish(part: Path, dest: Path, taken_at: datetime | None) -> None:
    os.replace(part, dest)
    if taken_at is not None:
        ts = taken_at.timestamp()
        os.utime(dest, (ts, ts))


def fetch_version(
    source: PhotoSource, asset: RemoteAsset, role: str, version: RemoteVersion, dest: Path
) -> tuple[int, str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = _part_path(dest)
    digest = hashlib.sha256()
    size = 0
    try:
        with open(part, "wb") as f:
            for chunk in source.download(asset, role):
                f.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            f.flush()
            os.fsync(f.fileno())
        if size != version.size:
            raise SourceError(f"{dest.name} 大小不符：下载 {size} 字节，iCloud 记录 {version.size} 字节")
        _finish(part, dest, asset.taken_at)
    finally:
        part.unlink(missing_ok=True)
    return size, digest.hexdigest()


def write_xmp(content: bytes, dest: Path, taken_at: datetime | None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = _part_path(dest)
    try:
        with open(part, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        _finish(part, dest, taken_at)
    finally:
        part.unlink(missing_ok=True)


def _file_ok(row: sqlite3.Row | None, dest: Path) -> bool:
    if row is None or row["sha256"] is None:
        return False
    try:
        return dest.stat().st_size == row["size"]
    except FileNotFoundError:
        return False


def _save_file(
    conn: sqlite3.Connection,
    account_id: int,
    asset_id: str,
    role: str,
    rel: str,
    expected_size: int,
    checksum: str | None,
    size: int,
    sha256: str,
) -> None:
    conn.execute(
        """
        INSERT INTO files (account_id, asset_id, role, rel_path, expected_size, remote_checksum,
                           size, sha256, downloaded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (account_id, asset_id, role) DO UPDATE SET
            rel_path = excluded.rel_path, expected_size = excluded.expected_size,
            remote_checksum = excluded.remote_checksum, size = excluded.size,
            sha256 = excluded.sha256, downloaded_at = excluded.downloaded_at
        """,
        (account_id, asset_id, role, rel, expected_size, checksum, size, sha256, utcnow()),
    )


def _get_file(conn: sqlite3.Connection, account_id: int, asset_id: str, role: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM files WHERE account_id = ? AND asset_id = ? AND role = ?",
        (account_id, asset_id, role),
    ).fetchone()


def _upsert_asset(conn: sqlite3.Connection, account_id: int, asset: RemoteAsset, month: str, now: str) -> bool:
    exists = conn.execute(
        "SELECT 1 FROM assets WHERE account_id = ? AND asset_id = ?", (account_id, asset.asset_id)
    ).fetchone()
    conn.execute(
        """
        INSERT INTO assets (account_id, asset_id, record_name, change_tag, filename, item_type,
                            taken_at, month, size, roles, state, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_icloud', ?, ?)
        ON CONFLICT (account_id, asset_id) DO UPDATE SET
            record_name = excluded.record_name, change_tag = excluded.change_tag,
            filename = excluded.filename, item_type = excluded.item_type,
            taken_at = excluded.taken_at, month = excluded.month, size = excluded.size,
            roles = excluded.roles, state = 'in_icloud', last_seen_at = excluded.last_seen_at,
            deleted_at = NULL
        """,
        (
            account_id,
            asset.asset_id,
            asset.record_name,
            asset.change_tag,
            asset.filename,
            asset.item_type,
            asset.taken_at.astimezone(UTC).isoformat(timespec="seconds") if asset.taken_at else None,
            month,
            asset.versions["original"].size,
            ",".join(expected_roles(asset)),
            now,
            now,
        ),
    )
    return exists is None


def backup_asset(
    db: Database, account_id: int, account_dir: Path, source: PhotoSource, asset: RemoteAsset, month: str
) -> tuple[int, int]:
    """下载这张照片缺少或已变化的文件，返回下载的文件数和字节数。"""
    with db.connect() as conn:
        paths = assign_paths(conn, account_id, account_dir, asset, month)
    files = 0
    total = 0
    for role, version in asset.versions.items():
        rel = paths[role]
        dest = account_dir / rel
        with db.connect() as conn:
            row = _get_file(conn, account_id, asset.asset_id, role)
        if _file_ok(row, dest) and row["remote_checksum"] == version.checksum:
            continue
        size, sha = fetch_version(source, asset, role, version, dest)
        with db.connect() as conn:
            _save_file(conn, account_id, asset.asset_id, role, rel, version.size, version.checksum, size, sha)
        files += 1
        total += size
    if asset.xmp is not None:
        rel = paths["xmp"]
        dest = account_dir / rel
        sha = hashlib.sha256(asset.xmp).hexdigest()
        with db.connect() as conn:
            row = _get_file(conn, account_id, asset.asset_id, "xmp")
        if not (_file_ok(row, dest) and row["sha256"] == sha):
            write_xmp(asset.xmp, dest, asset.taken_at)
            with db.connect() as conn:
                _save_file(conn, account_id, asset.asset_id, "xmp", rel, len(asset.xmp), None, len(asset.xmp), sha)
    return files, total


def sync_account(
    db: Database,
    backup_root: Path,
    tz: ZoneInfo,
    account: sqlite3.Row,
    source: PhotoSource,
    progress: Progress = no_progress,
) -> SyncResult:
    """列出账号的全部照片，下载缺少的文件，并标记在 iCloud 中已经不存在的照片。"""
    result = SyncResult()
    account_id = account["id"]
    account_dir = backup_root / account["name"]
    account_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    now = utcnow()
    # 先查总数，进度才有分母；查不到时照常同步
    try:
        count = source.count()
    except SourceError as e:
        log.warning("%s 查询照片数量失败：%s", account["name"], e)
        total = None
    else:
        with db.connect() as conn:
            conn.execute(
                "UPDATE accounts SET icloud_total = ?, icloud_videos = ?, counted_at = ? WHERE id = ?",
                (count.total, count.videos, now, account_id),
            )
        total = count.total
        progress(f"iCloud 共 {count.total} 张，其中视频 {count.videos} 个，开始检查", 0, total)
    for asset in source.iter_assets():
        result.listed += 1
        seen.add(asset.asset_id)
        month = month_key(asset.taken_at, tz)
        with db.connect() as conn:
            if _upsert_asset(conn, account_id, asset, month, now):
                result.new_assets += 1
        try:
            files, size = backup_asset(db, account_id, account_dir, source, asset, month)
        except (SourceError, OSError, sqlite3.Error) as e:
            result.add_error(f"{asset.filename}：{e}")
            continue
        result.downloaded += files
        result.downloaded_bytes += size
        if result.listed % 50 == 0:
            of_total = f" / {total}" if total else ""
            progress(f"已检查 {result.listed}{of_total} 张，下载 {result.downloaded} 个文件", result.listed, total)

    # 只有完整列完照片库之后，才能判断哪些照片已经不在 iCloud 里
    seen.update(getattr(source, "unreadable", ()))
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT asset_id FROM assets WHERE account_id = ? AND state = 'in_icloud'", (account_id,)
        ).fetchall()
        gone = [(account_id, r["asset_id"]) for r in rows if r["asset_id"] not in seen]
        conn.executemany("UPDATE assets SET state = 'gone' WHERE account_id = ? AND asset_id = ?", gone)
        result.gone = len(gone)
        error = "；".join(result.errors[:3]) if result.errors else None
        conn.execute(
            "UPDATE accounts SET last_sync_at = ?, last_sync_error = ?, auth_state = 'ok' WHERE id = ?",
            (utcnow(), error, account_id),
        )
    return result
