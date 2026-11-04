"""后台任务：同一时间只运行一个任务，并按设定的间隔定时运行完整一轮。"""

import logging
import threading
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .archive import run_archive
from .backup import sync_account
from .config import Config
from .db import Database, utcnow
from .deletion import auto_delete_plan, execute_deletion
from .source import Backend, LoginRequired, PhotoSource

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


@dataclass
class JobRecord:
    name: str
    started_at: str
    finished_at: str | None = None
    summary: str = ""
    ok: bool = True


class Jobs:
    def __init__(self, db: Database, cfg: Config, backend: Backend, on_finish: Callable[[], None] | None = None):
        self.db = db
        self.cfg = cfg
        self.backend = backend
        # 任务结束后调用，网页用它让统计缓存失效
        self.on_finish = on_finish
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.current: JobRecord | None = None
        self.progress = ""
        # 正在同步第几个账号，给状态栏显示
        self.syncing: dict | None = None
        self.history: deque[JobRecord] = deque(maxlen=20)

    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def submit(self, name: str, fn: Callable[[Progress], str]) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            record = JobRecord(name=name, started_at=utcnow())
            self.current = record
            self.progress = "开始运行"
            self._thread = threading.Thread(target=self._run, args=(record, fn), daemon=True)
            self._thread.start()
            return True

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _set_progress(self, message: str) -> None:
        self.progress = message

    def _run(self, record: JobRecord, fn: Callable[[Progress], str]) -> None:
        try:
            record.summary = fn(self._set_progress)
        except Exception as e:
            log.error("任务 %s 失败：%s", record.name, traceback.format_exc())
            record.summary = f"失败：{e}"
            record.ok = False
        record.finished_at = utcnow()
        if self.on_finish is not None:
            try:
                self.on_finish()
            except Exception:
                log.exception("任务结束后的回调失败")
        with self._lock:
            self.history.appendleft(record)
            self.current = None
            self.progress = ""
            self.syncing = None

    # ---- 任务内容 ----

    def _accounts(self, account_id: int | None = None) -> list:
        with self.db.connect() as conn:
            if account_id is None:
                return conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
            return conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchall()

    def _open(self, account) -> PhotoSource | None:
        try:
            return self.backend.open_source(account["id"], account["domain"], account["apple_id"])
        except LoginRequired as e:
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE accounts SET auth_state = 'need_login', last_sync_error = ? WHERE id = ?",
                    (str(e), account["id"]),
                )
            return None

    def sync(self, progress: Progress, account_id: int | None = None) -> tuple[list[str], dict[int, PhotoSource]]:
        lines: list[str] = []
        sources: dict[int, PhotoSource] = {}
        accounts = self._accounts(account_id)
        for index, account in enumerate(accounts, 1):
            self.syncing = {"index": index, "total": len(accounts), "name": account["name"]}
            source = self._open(account)
            if source is None:
                lines.append(f"{account['name']}：需要重新登录，已跳过")
                continue
            progress(f"{account['name']}：正在列出 iCloud 照片")
            try:
                r = sync_account(self.db, self.cfg.backup_root, self.cfg.timezone, account, source, progress)
            except Exception as e:
                log.error("同步 %s 中断：%s", account["name"], traceback.format_exc())
                with self.db.connect() as conn:
                    conn.execute("UPDATE accounts SET last_sync_error = ? WHERE id = ?", (str(e), account["id"]))
                lines.append(f"{account['name']}：同步中断：{e}")
                continue
            sources[account["id"]] = source
            line = (
                f"{account['name']}：检查 {r.listed} 张，新增 {r.new_assets} 张，"
                f"下载 {r.downloaded} 个文件，{r.gone} 张已不在 iCloud"
            )
            if r.errors:
                line += f"，{len(r.errors)} 个错误"
            lines.append(line)
        self.syncing = None
        return lines, sources

    def archive(self, progress: Progress) -> list[str]:
        progress("正在归档")
        r = run_archive(self.db, self.cfg, datetime.now(UTC), progress)
        lines = [f"归档：新打包 {r.built} 个月，{r.unchanged} 个月无变化"]
        lines += [f"归档错误：{e}" for e in r.errors[:5]]
        return lines

    def auto_delete(self, progress: Progress, sources: dict[int, PhotoSource]) -> list[str]:
        if self.db.get_setting("auto_delete_mode") != "on":
            return []
        years = self.db.get_int("auto_delete_years")
        limit = self.db.get_int("auto_delete_max_per_run")
        lines = []
        now = datetime.now(UTC)
        for account in self._accounts():
            source = sources.get(account["id"])
            if source is None:
                continue
            plan = auto_delete_plan(
                self.db, self.cfg.backup_root, self.cfg.archive_root, account["id"], now, years, limit
            )
            if not plan:
                continue
            progress(f"{account['name']}：自动删除 {len(plan)} 张")
            r = execute_deletion(
                self.db, self.cfg.backup_root, self.cfg.archive_root, source, account["id"],
                [c.asset_id for c in plan], "auto", progress,
            )
            lines.append(f"{account['name']}：自动删除 {r.deleted} 张，跳过 {r.skipped} 张，失败 {len(r.errors)} 张")
        return lines

    def cycle(self, progress: Progress) -> str:
        lines, sources = self.sync(progress)
        lines += self.archive(progress)
        lines += self.auto_delete(progress, sources)
        self.db.set_setting("last_cycle_at", utcnow())
        return "\n".join(lines)

    def manual_delete(self, progress: Progress, account_id: int, asset_ids: list[str]) -> str:
        account = self._accounts(account_id)[0]
        source = self._open(account)
        if source is None:
            return f"{account['name']}：需要重新登录，没有删除任何照片"
        r = execute_deletion(
            self.db, self.cfg.backup_root, self.cfg.archive_root, source, account_id, asset_ids, "manual", progress
        )
        lines = [f"{account['name']}：人工确认删除 {r.deleted} 张，跳过 {r.skipped} 张，失败 {len(r.errors)} 张"]
        lines += r.errors[:5]
        return "\n".join(lines)


class Scheduler:
    """每分钟检查一次，到了设定的间隔就运行完整一轮。"""

    def __init__(self, jobs: Jobs, check_seconds: float = 60):
        self.jobs = jobs
        self.check_seconds = check_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def due(self, now: datetime) -> bool:
        last = self.jobs.db.get_setting("last_cycle_at")
        if not last:
            return True
        interval = timedelta(hours=self.jobs.db.get_int("sync_interval_hours"))
        return now - datetime.fromisoformat(last) >= interval

    def _loop(self) -> None:
        while not self._stop.wait(self.check_seconds):
            if not self.jobs.running() and self.due(datetime.now(UTC)):
                self.jobs.submit("定时运行", self.jobs.cycle)
