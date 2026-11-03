"""按账号和月份把备份打包成加密的 7z 归档，校验通过后放进归档目录。"""

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .backup import UNKNOWN_MONTH, month_key
from .config import Config
from .db import Database, utcnow

log = logging.getLogger(__name__)

READ_CHUNK = 1 << 20


class ArchiveError(Exception):
    pass


@dataclass(frozen=True)
class ManifestEntry:
    rel_path: str
    size: int
    sha256: str


@dataclass
class ArchiveRunResult:
    built: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)


def read_password(cfg: Config) -> str | None:
    try:
        password = cfg.password_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return password or None


def write_password(cfg: Config, password: str) -> None:
    """归档密码只能设置一次，否则旧包和新包会用不同的密码。"""
    if read_password(cfg) is not None:
        raise ArchiveError("归档密码已经设置过，不能修改")
    if len(password) < 12:
        raise ArchiveError("归档密码至少需要 12 个字符")
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg.password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(password)


def month_manifest(conn: sqlite3.Connection, account_id: int, month: str) -> list[ManifestEntry]:
    rows = conn.execute(
        """
        SELECT f.rel_path, f.size, f.sha256 FROM files f
        JOIN assets a ON a.account_id = f.account_id AND a.asset_id = f.asset_id
        WHERE f.account_id = ? AND a.month = ? AND f.sha256 IS NOT NULL
        ORDER BY f.rel_path
        """,
        (account_id, month),
    ).fetchall()
    return [ManifestEntry(r["rel_path"], r["size"], r["sha256"]) for r in rows]


def manifest_hash(entries: list[ManifestEntry]) -> str:
    digest = hashlib.sha256()
    for e in entries:
        digest.update(f"{e.rel_path}\t{e.size}\t{e.sha256}\n".encode())
    return digest.hexdigest()


def archivable_months(conn: sqlite3.Connection, account_id: int, current_month: str) -> list[str]:
    """已经结束的月份，以及没有拍摄日期的照片。"""
    rows = conn.execute(
        """
        SELECT DISTINCT a.month FROM assets a
        JOIN files f ON a.account_id = f.account_id AND a.asset_id = f.asset_id
        WHERE a.account_id = ? AND f.sha256 IS NOT NULL ORDER BY a.month
        """,
        (account_id,),
    ).fetchall()
    return [r["month"] for r in rows if r["month"] == UNKNOWN_MONTH or r["month"] < current_month]


class SevenZip:
    def __init__(self, executable: str):
        self.executable = executable

    def _run(self, args: list[str], cwd: Path | None = None) -> str:
        proc = subprocess.run(
            [self.executable, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8"
        )
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout).strip().splitlines()
            raise ArchiveError(f"7-Zip 返回 {proc.returncode}：{' '.join(message[-3:])}")
        return proc.stdout

    def create(self, cwd: Path, archive: Path, list_file: Path, password: str, volume_mb: int) -> None:
        # 照片和视频本身已经压缩过，只存储不压缩；-mhe=on 连文件名一起加密
        self._run(
            [
                "a", "-t7z", "-mx=0", "-mhe=on", f"-p{password}", f"-v{volume_mb}m",
                "-scsUTF-8", "-bd", "-y", str(archive), f"@{list_file}",
            ],
            cwd=cwd,
        )

    def list_files(self, first_volume: Path, password: str) -> list[tuple[str, int]]:
        out = self._run(["l", "-slt", "-ba", f"-p{password}", str(first_volume)])
        entries: list[tuple[str, int]] = []
        current: dict[str, str] = {}
        for line in [*out.splitlines(), ""]:
            if not line.strip():
                if "Path" in current and not current.get("Attributes", "").startswith("D"):
                    entries.append((current["Path"], int(current.get("Size", "0") or 0)))
                current = {}
                continue
            key, sep, value = line.partition(" = ")
            if sep:
                if key == "Path" and "Path" in current:
                    if not current.get("Attributes", "").startswith("D"):
                        entries.append((current["Path"], int(current.get("Size", "0") or 0)))
                    current = {}
                current[key] = value
        return entries

    def open_stream(self, first_volume: Path, password: str, stderr) -> subprocess.Popen:
        return subprocess.Popen(
            [self.executable, "x", "-so", f"-p{password}", "-bd", str(first_volume)],
            stdout=subprocess.PIPE,
            stderr=stderr,
        )


def verify_archive(sz: SevenZip, first_volume: Path, password: str, entries: list[ManifestEntry]) -> None:
    """解开整个归档，逐个核对文件的路径、大小和 SHA-256。"""
    expected = {e.rel_path: e for e in entries}
    listed = sz.list_files(first_volume, password)
    listed_paths = [p for p, _ in listed]
    if sorted(listed_paths) != sorted(expected):
        missing = set(expected) - set(listed_paths)
        extra = set(listed_paths) - set(expected)
        raise ArchiveError(f"归档内容与清单不一致：缺少 {len(missing)} 个，多出 {len(extra)} 个")
    for path, size in listed:
        if size != expected[path].size:
            raise ArchiveError(f"{path} 大小不符")
    with tempfile.TemporaryFile() as stderr:
        proc = sz.open_stream(first_volume, password, stderr)
        assert proc.stdout is not None
        try:
            for path, size in listed:
                digest = hashlib.sha256()
                remaining = size
                while remaining:
                    chunk = proc.stdout.read(min(READ_CHUNK, remaining))
                    if not chunk:
                        raise ArchiveError(f"解包 {path} 时数据提前结束")
                    digest.update(chunk)
                    remaining -= len(chunk)
                if digest.hexdigest() != expected[path].sha256:
                    raise ArchiveError(f"{path} 的 SHA-256 不符")
            if proc.stdout.read(1):
                raise ArchiveError("解包输出比清单多")
        finally:
            proc.stdout.close()
            code = proc.wait()
        if code != 0:
            stderr.seek(0)
            raise ArchiveError(f"7-Zip 解包返回 {code}：{stderr.read().decode(errors='replace').strip()[-200:]}")


def _year_dir(month: str) -> str:
    return UNKNOWN_MONTH if month == UNKNOWN_MONTH else month[:4]


def volumes_present(archive_root: Path, volumes_json: str) -> bool:
    return all((archive_root / name).is_file() for name in json.loads(volumes_json))


def build_month_archive(
    db: Database, cfg: Config, sz: SevenZip, account: sqlite3.Row, month: str, password: str, volume_mb: int
) -> bool:
    """需要时重新打包这个月，返回是否生成了新包。"""
    account_id = account["id"]
    with db.connect() as conn:
        entries = month_manifest(conn, account_id, month)
        existing = conn.execute(
            "SELECT * FROM archives WHERE account_id = ? AND month = ?", (account_id, month)
        ).fetchone()
    if not entries:
        return False
    digest = manifest_hash(entries)
    if existing and existing["manifest_sha256"] == digest and volumes_present(cfg.archive_root, existing["volumes"]):
        return False

    version = existing["version"] + 1 if existing else 1
    base = f"{account['name']}-{month}-v{version}.7z"
    stage = cfg.staging_dir / base
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    try:
        list_file = stage / "files.txt"
        list_file.write_text("\n".join(e.rel_path for e in entries) + "\n", encoding="utf-8")
        sz.create(cfg.backup_root / account["name"], stage / base, list_file, password, volume_mb)
        volumes = sorted(p for p in stage.iterdir() if p.name.startswith(base))
        if not volumes:
            raise ArchiveError("7-Zip 没有生成归档文件")
        verify_archive(sz, volumes[0], password, entries)

        final_dir = cfg.archive_root / account["name"] / _year_dir(month)
        final_dir.mkdir(parents=True, exist_ok=True)
        names: list[str] = []
        for volume in volumes:
            partial = final_dir / f".{volume.name}.partial"
            shutil.move(volume, partial)
            os.replace(partial, final_dir / volume.name)
            names.append((final_dir / volume.name).relative_to(cfg.archive_root).as_posix())
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO archives (account_id, month, version, manifest_sha256, volumes, file_count,
                                  total_size, verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (account_id, month) DO UPDATE SET
                version = excluded.version, manifest_sha256 = excluded.manifest_sha256,
                volumes = excluded.volumes, file_count = excluded.file_count,
                total_size = excluded.total_size, verified_at = excluded.verified_at
            """,
            (
                account_id, month, version, digest, json.dumps(names), len(entries),
                sum(e.size for e in entries), utcnow(),
            ),
        )
        conn.execute("DELETE FROM archive_members WHERE account_id = ? AND month = ?", (account_id, month))
        conn.executemany(
            "INSERT INTO archive_members (account_id, month, rel_path, size, sha256) VALUES (?, ?, ?, ?, ?)",
            [(account_id, month, e.rel_path, e.size, e.sha256) for e in entries],
        )
    # 新包已经登记，旧版本的包可以移除了
    if existing:
        for name in json.loads(existing["volumes"]):
            if name not in names:
                (cfg.archive_root / name).unlink(missing_ok=True)
    return True


def run_archive(
    db: Database, cfg: Config, now: datetime, progress: Callable[[str], None] = lambda _: None
) -> ArchiveRunResult:
    result = ArchiveRunResult()
    password = read_password(cfg)
    if password is None:
        result.errors.append("还没有设置归档密码，跳过归档")
        return result
    sz = SevenZip(cfg.sevenzip)
    volume_mb = db.get_int("archive_volume_mb")
    current_month = month_key(now, cfg.timezone)
    with db.connect() as conn:
        accounts = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    for account in accounts:
        with db.connect() as conn:
            months = archivable_months(conn, account["id"], current_month)
        for month in months:
            try:
                if build_month_archive(db, cfg, sz, account, month, password, volume_mb):
                    result.built += 1
                    progress(f"{account['name']}：已归档 {month}")
                else:
                    result.unchanged += 1
            except (ArchiveError, OSError) as e:
                message = f"{account['name']} {month}：{e}"
                log.warning(message)
                result.errors.append(message)
    return result
