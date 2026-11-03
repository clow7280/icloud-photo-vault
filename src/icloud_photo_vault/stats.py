"""界面用的统计，只读数据库，不检查磁盘。"""

import sqlite3

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
