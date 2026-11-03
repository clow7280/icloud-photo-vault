"""判断照片的副本数量，生成删除清单，并从 iCloud 删除。"""

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .db import Database, utcnow
from .source import PhotoSource, SourceError

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_ERRORS = 5


@dataclass
class Candidate:
    account_id: int
    asset_id: str
    record_name: str
    change_tag: str | None
    filename: str
    taken_at: str | None
    month: str
    size: int
    copies: int
    problem: str | None

    @property
    def backed_up(self) -> bool:
        return self.copies >= 1


@dataclass
class DeleteResult:
    deleted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


class CopyChecker:
    """按备份目录和归档登记检查一张照片有几份副本。"""

    def __init__(self, conn: sqlite3.Connection, backup_root: Path, archive_root: Path):
        self.conn = conn
        self.backup_root = backup_root
        self.archive_root = archive_root
        self._archive_ok: dict[tuple[int, str], bool] = {}
        self._account_dirs: dict[int, Path] = {}

    def _account_dir(self, account_id: int) -> Path:
        if account_id not in self._account_dirs:
            row = self.conn.execute("SELECT name FROM accounts WHERE id = ?", (account_id,)).fetchone()
            self._account_dirs[account_id] = self.backup_root / row["name"]
        return self._account_dirs[account_id]

    def _archive_present(self, account_id: int, month: str) -> bool:
        key = (account_id, month)
        if key not in self._archive_ok:
            row = self.conn.execute(
                "SELECT volumes FROM archives WHERE account_id = ? AND month = ?", key
            ).fetchone()
            self._archive_ok[key] = row is not None and all(
                (self.archive_root / name).is_file() for name in json.loads(row["volumes"])
            )
        return self._archive_ok[key]

    def check(self, asset: sqlite3.Row) -> tuple[int, str | None]:
        """返回副本数量和不满足条件的原因。"""
        if asset["taken_at"] is None:
            return 0, "没有拍摄日期"
        account_id = asset["account_id"]
        files = {
            r["role"]: r
            for r in self.conn.execute(
                "SELECT * FROM files WHERE account_id = ? AND asset_id = ?", (account_id, asset["asset_id"])
            )
        }
        account_dir = self._account_dir(account_id)
        for role in asset["roles"].split(","):
            row = files.get(role)
            if row is None or row["sha256"] is None:
                return 0, f"缺少 {role} 文件"
            if role != "xmp" and row["size"] != row["expected_size"]:
                return 0, f"{role} 文件大小与 iCloud 记录不符"
            try:
                on_disk = (account_dir / row["rel_path"]).stat().st_size
            except FileNotFoundError:
                return 0, f"NAS 上找不到 {row['rel_path']}"
            if on_disk != row["size"]:
                return 0, f"{row['rel_path']} 在 NAS 上的大小变了"

        if not self._archive_present(account_id, asset["month"]):
            return 1, "所在月份还没有归档"
        members = {
            r["rel_path"]: r
            for r in self.conn.execute(
                "SELECT rel_path, size, sha256 FROM archive_members WHERE account_id = ? AND month = ?",
                (account_id, asset["month"]),
            )
        }
        for role in asset["roles"].split(","):
            row = files[role]
            member = members.get(row["rel_path"])
            if member is None or member["sha256"] != row["sha256"]:
                return 1, "归档包里的文件不是最新的"
        return 2, None


def _candidate(asset: sqlite3.Row, copies: int, problem: str | None) -> Candidate:
    return Candidate(
        account_id=asset["account_id"],
        asset_id=asset["asset_id"],
        record_name=asset["record_name"],
        change_tag=asset["change_tag"],
        filename=asset["filename"],
        taken_at=asset["taken_at"],
        month=asset["month"],
        size=asset["size"],
        copies=copies,
        problem=problem,
    )


def years_before(now: datetime, years: int) -> datetime:
    try:
        return now.replace(year=now.year - years)
    except ValueError:  # 2 月 29 日
        return now.replace(year=now.year - years, day=28)


def auto_delete_plan(
    db: Database, backup_root: Path, archive_root: Path, account_id: int, now: datetime, years: int, limit: int
) -> list[Candidate]:
    """拍摄日期早于指定年限、并且有两份副本的照片，按拍摄时间从早到晚排列。"""
    # taken_at 统一以 UTC 的 ISO 字符串保存，可以直接按字符串比较
    cutoff = years_before(now.astimezone(UTC), years).isoformat(timespec="seconds")
    plan: list[Candidate] = []
    with db.connect() as conn:
        checker = CopyChecker(conn, backup_root, archive_root)
        rows = conn.execute(
            """
            SELECT * FROM assets WHERE account_id = ? AND state = 'in_icloud'
              AND taken_at IS NOT NULL AND taken_at < ? ORDER BY taken_at
            """,
            (account_id, cutoff),
        )
        for asset in rows:
            copies, problem = checker.check(asset)
            if copies == 2:
                plan.append(_candidate(asset, copies, problem))
                if len(plan) >= limit:
                    break
    return plan


def cutoff_month(now: datetime, years: int, tz: ZoneInfo) -> str:
    """自动清理的分界月份：早于这个月的照片全部超出保留期，这个月本身部分超出。"""
    return years_before(now, years).astimezone(tz).strftime("%Y-%m")


def manual_delete_preview(
    db: Database, backup_root: Path, archive_root: Path, account_id: int, months: list[str]
) -> list[Candidate]:
    """所选月份中仍在 iCloud 的照片，逐张检查副本。月份按本地时区划分，与界面一致。"""
    result: list[Candidate] = []
    with db.connect() as conn:
        checker = CopyChecker(conn, backup_root, archive_root)
        marks = ",".join("?" * len(months))
        rows = conn.execute(
            f"""
            SELECT * FROM assets WHERE account_id = ? AND state = 'in_icloud'
              AND month IN ({marks}) ORDER BY taken_at
            """,
            (account_id, *months),
        )
        for asset in rows:
            copies, problem = checker.check(asset)
            result.append(_candidate(asset, copies, problem))
    return result


def execute_deletion(
    db: Database,
    backup_root: Path,
    archive_root: Path,
    source: PhotoSource,
    account_id: int,
    asset_ids: list[str],
    mode: str,
    progress: Callable[[str], None] = lambda _: None,
) -> DeleteResult:
    """删除前逐张重新检查：自动删除要求两份副本，人工删除要求至少已经备份。"""
    required = 2 if mode == "auto" else 1
    result = DeleteResult()
    consecutive_errors = 0
    for asset_id in asset_ids:
        with db.connect() as conn:
            asset = conn.execute(
                "SELECT * FROM assets WHERE account_id = ? AND asset_id = ?", (account_id, asset_id)
            ).fetchone()
            if asset is None or asset["state"] != "in_icloud":
                result.skipped += 1
                continue
            copies, problem = CopyChecker(conn, backup_root, archive_root).check(asset)
        if copies < required:
            result.skipped += 1
            log.info("跳过 %s：%s", asset["filename"], problem)
            continue
        try:
            source.delete(asset["record_name"], asset["change_tag"])
        except SourceError as e:
            consecutive_errors += 1
            result.errors.append(f"{asset['filename']}：{e}")
            _log_deletion(db, asset, mode, "error", str(e))
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                result.errors.append(f"连续失败 {MAX_CONSECUTIVE_ERRORS} 次，停止本轮删除")
                break
            continue
        consecutive_errors = 0
        with db.connect() as conn:
            conn.execute(
                "UPDATE assets SET state = 'deleted', deleted_at = ? WHERE account_id = ? AND asset_id = ?",
                (utcnow(), account_id, asset_id),
            )
        _log_deletion(db, asset, mode, "ok", None)
        result.deleted += 1
        if result.deleted % 50 == 0:
            progress(f"已从 iCloud 删除 {result.deleted} 张")
    return result


def _log_deletion(db: Database, asset: sqlite3.Row, mode: str, outcome: str, error: str | None) -> None:
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO deletions (account_id, asset_id, filename, taken_at, mode, result, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (asset["account_id"], asset["asset_id"], asset["filename"], asset["taken_at"], mode, outcome, error, utcnow()),
        )
