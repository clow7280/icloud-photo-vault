"""界面用的统计，只读数据库，不检查磁盘。"""

import logging
import sqlite3
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

ROLE_COUNT = "(LENGTH(a.roles) - LENGTH(REPLACE(a.roles, ',', '')) + 1)"

BACKED = f"COALESCE(f.n_ok, 0) = {ROLE_COUNT}"
TWO = f"{BACKED} AND COALESCE(f.n_arch, 0) = {ROLE_COUNT}"

# :cutoff 是自动清理的分界时间（UTC 的 ISO 字符串），早于它的两份副本照片可以自动清理
MONTH_STATS = f"""
WITH f AS (
    SELECT f.account_id, f.asset_id,
           COUNT(*) AS n_ok,
           SUM(CASE WHEN m.sha256 = f.sha256 THEN 1 ELSE 0 END) AS n_arch,
           SUM(f.size) AS bytes
    FROM files f
    JOIN assets a ON a.account_id = f.account_id AND a.asset_id = f.asset_id
    LEFT JOIN archive_members m
           ON m.account_id = f.account_id AND m.month = a.month AND m.rel_path = f.rel_path
    WHERE f.account_id = :account AND f.sha256 IS NOT NULL
      AND (f.role = 'xmp' OR f.size = f.expected_size)
      -- 只算照片现在应有的文件：编辑在 iCloud 里被还原后，旧编辑版的记录还在，但不算数
      AND INSTR(',' || a.roles || ',', ',' || f.role || ',') > 0
    GROUP BY f.account_id, f.asset_id
)
SELECT a.month AS month,
       COUNT(*) AS total,
       SUM(a.state = 'in_icloud') AS in_icloud,
       SUM(a.state = 'deleted') AS deleted,
       SUM(a.state = 'gone') AS gone,
       SUM(a.state = 'in_icloud' AND a.item_type = 'movie') AS videos,
       SUM(a.state = 'in_icloud' AND {BACKED}) AS backed_up,
       SUM(a.state = 'in_icloud' AND {TWO}) AS two_copies,
       SUM(a.state = 'in_icloud' AND {TWO} AND a.taken_at IS NOT NULL AND a.taken_at < :cutoff) AS auto_ready,
       SUM(CASE WHEN a.state = 'in_icloud' THEN a.size ELSE 0 END) AS icloud_bytes,
       SUM(CASE WHEN a.state = 'in_icloud' AND a.item_type = 'movie' THEN a.size ELSE 0 END) AS video_bytes,
       SUM(CASE WHEN a.state = 'in_icloud' AND {BACKED} THEN a.size ELSE 0 END) AS backed_bytes,
       SUM(CASE WHEN a.state = 'in_icloud' AND {TWO} THEN a.size ELSE 0 END) AS two_copies_bytes,
       SUM(CASE WHEN a.state = 'in_icloud' AND {TWO} AND a.taken_at IS NOT NULL AND a.taken_at < :cutoff THEN a.size ELSE 0 END) AS auto_bytes,
       SUM(CASE WHEN a.state = 'deleted' THEN a.size ELSE 0 END) AS deleted_bytes,
       SUM(COALESCE(f.bytes, 0)) AS nas_bytes
FROM assets a LEFT JOIN f ON f.account_id = a.account_id AND f.asset_id = a.asset_id
WHERE a.account_id = :account
GROUP BY a.month ORDER BY a.month DESC
"""

FIELDS = (
    "total", "in_icloud", "deleted", "gone", "videos", "backed_up", "two_copies", "auto_ready",
    "icloud_bytes", "video_bytes", "backed_bytes", "two_copies_bytes", "auto_bytes", "deleted_bytes", "nas_bytes",
)


def month_stats(conn: sqlite3.Connection, account_id: int, cutoff: str = "") -> list[dict]:
    archives = {
        r["month"]: dict(r)
        for r in conn.execute("SELECT * FROM archives WHERE account_id = ?", (account_id,))
    }
    rows = []
    for r in conn.execute(MONTH_STATS, {"account": account_id, "cutoff": cutoff}):
        row = dict(r)
        row["archive"] = archives.get(row["month"])
        rows.append(row)
    return rows


def totals(rows: list[dict]) -> dict:
    return {key: sum(r[key] or 0 for r in rows) for key in FIELDS}


def year_stats(rows: list[dict]) -> list[dict]:
    """把按月统计合并成按年统计，没有拍摄日期的照片单独一组，排在最后。"""
    years: dict[str, dict] = {}
    for r in rows:
        key = r["month"][:4] if r["month"] != "unknown" else "unknown"
        acc = years.setdefault(key, {"year": key, **dict.fromkeys(FIELDS, 0)})
        for field in FIELDS:
            acc[field] += r[field] or 0
    return sorted(years.values(), key=lambda y: (y["year"] != "unknown", y["year"]), reverse=True)


class StatsCache:
    """缓存每个账号的按月统计，让页面不必每次都等统计查询。

    照片多、NAS 又在同步时，一次统计要几秒，窗口会一直白屏。
    缓存超过 max_age 秒或分界时间变了，先返回旧结果，同时在后台重算；
    还没有缓存时才当场计算。任务结束或规则变化后调用 invalidate，下次当场重算。
    """

    def __init__(self, connect: Callable[[], sqlite3.Connection], max_age: float = 60):
        self._connect = connect
        self.max_age = max_age
        self._lock = threading.Lock()
        self._entries: dict[int, tuple[str, float, list[dict]]] = {}
        self._refreshing: set[int] = set()
        # 每次 invalidate 加一，作废在这之前开始的后台重算
        self._generation = 0

    def get(self, account_id: int, cutoff: str) -> list[dict]:
        with self._lock:
            entry = self._entries.get(account_id)
            generation = self._generation
            if entry is not None:
                fresh = entry[0] == cutoff and time.monotonic() - entry[1] < self.max_age
                if not fresh and account_id not in self._refreshing:
                    self._refreshing.add(account_id)
                    threading.Thread(
                        target=self._refresh, args=(account_id, cutoff, generation), daemon=True
                    ).start()
                return entry[2]
        return self._compute(account_id, cutoff, generation)

    def warm(self, account_ids: list[int], cutoff: str) -> None:
        """在后台把这些账号的统计先算好，服务启动时调用。"""
        def run() -> None:
            for account_id in account_ids:
                try:
                    self.get(account_id, cutoff)
                except Exception:
                    log.exception("预先计算账号 %s 的统计失败", account_id)
        threading.Thread(target=run, daemon=True).start()

    def invalidate(self) -> None:
        with self._lock:
            self._entries.clear()
            self._generation += 1

    def _compute(self, account_id: int, cutoff: str, generation: int) -> list[dict]:
        with self._connect() as conn:
            rows = month_stats(conn, account_id, cutoff)
        with self._lock:
            if generation == self._generation:
                self._entries[account_id] = (cutoff, time.monotonic(), rows)
        return rows

    def _refresh(self, account_id: int, cutoff: str, generation: int) -> None:
        try:
            self._compute(account_id, cutoff, generation)
        except Exception:
            log.exception("重算账号 %s 的统计失败", account_id)
        finally:
            with self._lock:
                self._refreshing.discard(account_id)
