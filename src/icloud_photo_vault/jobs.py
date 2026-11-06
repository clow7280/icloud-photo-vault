"""后台任务：按冲突关系排队运行，并按设定的间隔定时运行完整一轮。

同步、归档这类重任务占用网络、CPU 和 Apple 的请求额度，一次只运行一个。
人工删除、改名这类轻任务只用到一个账号，和正在运行、正在排队的任务都不冲突时马上运行。
"""

import hashlib
import itertools
import logging
import threading
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .archive import run_archive
from .backup import sync_account
from .config import Config
from .db import Database, utcnow
from .deletion import auto_delete_plan, execute_deletion
from .source import Backend, LoginRequired, PhotoSource

log = logging.getLogger(__name__)

HISTORY_SIZE = 20
# 任务结果里列出的失败照片数量
ERRORS_IN_SUMMARY = 10


@dataclass(eq=False)
class Task:
    """一个任务。本身可以当作进度回调传给同步、归档和删除。"""

    id: int
    name: str
    fn: Callable[["Task"], str]
    # 去重用：同一个 key 正在运行或排队时不再加入。None 表示不去重
    key: str | None
    # 用到的账号，None 表示所有账号
    accounts: frozenset[int] | None
    heavy: bool
    queued_at: str
    # queued 排队中，running 运行中，done 完成，failed 失败，cancelled 已取消
    state: str = "queued"
    started_at: str | None = None
    finished_at: str | None = None
    summary: str = ""
    step: str = ""
    # 进度条用的完成数和总数，不知道时为 None
    meter: tuple[int, int] | None = None
    # 正在同步第几个账号，给状态栏显示
    syncing: dict | None = None
    finished: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def ok(self) -> bool:
        return self.state != "failed"

    def __call__(self, message: str, done: int | None = None, total: int | None = None) -> None:
        self.step = message
        self.meter = (done, total) if done is not None and total else None

    def conflicts(self, other: "Task") -> bool:
        if self.heavy and other.heavy:
            return True
        if self.accounts is None or other.accounts is None:
            return True
        return bool(self.accounts & other.accounts)


@dataclass
class Submission:
    # started 已开始，queued 已排队，duplicate 同样的任务已经在运行或排队，busy 有冲突且不能排队
    outcome: str
    task: Task
    # 排队时是队列中的第几个
    position: int = 0


class Jobs:
    def __init__(self, db: Database, cfg: Config, backend: Backend, on_finish: Callable[[], None] | None = None):
        self.db = db
        self.cfg = cfg
        self.backend = backend
        # 每个任务结束后调用，网页用它让统计缓存失效
        self.on_finish = on_finish
        self._cond = threading.Condition()
        self._ids = itertools.count(1)
        self._running: list[Task] = []
        self._queue: list[Task] = []
        self._history: deque[Task] = deque(maxlen=HISTORY_SIZE)
        # 服务启动以来结束的任务数，网页用它判断要不要提示刷新
        self.finished_count = 0

    # ---- 排队和运行 ----

    def submit(
        self,
        name: str,
        fn: Callable[[Task], str],
        *,
        key: str | None = None,
        accounts: frozenset[int] | None = None,
        heavy: bool = True,
        queue: bool = True,
    ) -> Submission:
        """不指定账号和轻重时按最保守的方式处理：用到所有账号，和其他任务都冲突。"""
        with self._cond:
            if key is not None:
                for other in self._running + self._queue:
                    if other.key == key:
                        return Submission("duplicate", other)
            task = Task(next(self._ids), name, fn, key, accounts, heavy, utcnow())
            blocked = any(task.conflicts(other) for other in self._running + self._queue)
            if blocked and not queue:
                return Submission("busy", task)
            self._queue.append(task)
            self._dispatch()
            # 不能看 task.state：很快的任务这时可能已经运行完了
            if task in self._queue:
                return Submission("queued", task, self._queue.index(task) + 1)
            return Submission("started", task)

    def _dispatch(self) -> None:
        """按排队顺序启动可以运行的任务：不能和正在运行的任务冲突，也不能和排在前面的任务冲突。

        调用方要持有 self._cond。
        """
        blockers = list(self._running)
        for task in list(self._queue):
            if not any(task.conflicts(other) for other in blockers):
                self._queue.remove(task)
                task.state = "running"
                task.started_at = utcnow()
                task.step = "开始运行"
                self._running.append(task)
                threading.Thread(target=self._run, args=(task,), daemon=True, name=f"task-{task.id}").start()
            blockers.append(task)

    def _run(self, task: Task) -> None:
        try:
            task.summary = task.fn(task)
            task.state = "done"
        except Exception as e:
            log.error("任务 %s 失败：%s", task.name, traceback.format_exc())
            task.summary = f"失败：{e}"
            task.state = "failed"
        task.finished_at = utcnow()
        task.step, task.meter, task.syncing = "", None, None
        if self.on_finish is not None:
            try:
                self.on_finish()
            except Exception:
                log.exception("任务结束后的回调失败")
        with self._cond:
            self._running.remove(task)
            self._history.appendleft(task)
            self.finished_count += 1
            self._dispatch()
            self._cond.notify_all()
        task.finished.set()

    def cancel(self, task_id: int) -> Task | None:
        """取消排队中的任务。已经开始运行的任务不能取消，返回 None。"""
        with self._cond:
            task = next((t for t in self._queue if t.id == task_id), None)
            if task is None:
                return None
            self._queue.remove(task)
            task.state = "cancelled"
            task.finished_at = utcnow()
            task.summary = "排队时被取消"
            self._history.appendleft(task)
            self.finished_count += 1
            # 被取消的任务可能挡着后面的任务
            self._dispatch()
            self._cond.notify_all()
        task.finished.set()
        return task

    def busy(self) -> bool:
        with self._cond:
            return bool(self._running or self._queue)

    def snapshot(self) -> tuple[list[Task], list[Task], list[Task]]:
        """正在运行、排队中和最近结束的任务，按显示顺序排列。"""
        with self._cond:
            return list(self._running), list(self._queue), list(self._history)

    @property
    def history(self) -> list[Task]:
        with self._cond:
            return list(self._history)

    def wait(self, timeout: float | None = None) -> bool:
        """等所有任务结束，包括排队中的。返回是否已经全部结束。"""
        with self._cond:
            return self._cond.wait_for(lambda: not self._running and not self._queue, timeout)

    # ---- 各类任务的提交方式 ----

    def submit_cycle(self, name: str = "完整运行") -> Submission:
        return self.submit(name, self.cycle, key="cycle")

    def submit_sync(self, account_id: int, account_name: str) -> Submission:
        return self.submit(
            f"同步 {account_name}",
            lambda task: "\n".join(self.sync(task, account_id)[0]),
            key=f"sync:{account_id}",
            accounts=frozenset({account_id}),
        )

    def submit_archive(self) -> Submission:
        return self.submit("归档", lambda task: "\n".join(self.archive(task)), key="archive")

    def submit_manual_delete(self, account_id: int, account_name: str, asset_ids: list[str]) -> Submission:
        """删除时逐张重新检查备份，所以可以排队，晚一点执行也安全。同一份清单重复提交只排一次。"""
        digest = hashlib.sha256("\n".join(sorted(asset_ids)).encode()).hexdigest()[:16]
        return self.submit(
            f"清理 {account_name}",
            lambda task: self.manual_delete(task, account_id, asset_ids),
            key=f"delete:{account_id}:{digest}",
            accounts=frozenset({account_id}),
            heavy=False,
        )

    def submit_rename(self, account_id: int, new_name: str, fn: Callable[[Task], str]) -> Submission:
        """改名要马上看到结果，不排队：有任务用到这个账号时直接拒绝。"""
        return self.submit(
            f"改名为 {new_name}", fn, key=f"rename:{account_id}", accounts=frozenset({account_id}),
            heavy=False, queue=False,
        )

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

    def sync(self, task: Task, account_id: int | None = None) -> tuple[list[str], dict[int, PhotoSource]]:
        lines: list[str] = []
        sources: dict[int, PhotoSource] = {}
        accounts = self._accounts(account_id)
        for index, account in enumerate(accounts, 1):
            task.syncing = {"index": index, "total": len(accounts), "name": account["name"]}
            source = self._open(account)
            if source is None:
                lines.append(f"{account['name']}：需要重新登录，已跳过")
                continue
            task("正在查询 iCloud 照片数量")
            try:
                r = sync_account(self.db, self.cfg.backup_root, self.cfg.timezone, account, source, task)
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
            if r.failed:
                line += f"，{r.failed} 张没有备份成功，下次同步会自动重试"
            lines.append(line)
            lines += [f"  {e}" for e in r.errors[:ERRORS_IN_SUMMARY]]
            if r.failed > ERRORS_IN_SUMMARY:
                lines.append(f"  另外 {r.failed - ERRORS_IN_SUMMARY} 张见服务日志")
        task.syncing = None
        return lines, sources

    def archive(self, progress: Task) -> list[str]:
        progress("正在归档")
        r = run_archive(self.db, self.cfg, datetime.now(UTC), progress)
        lines = [f"归档：新打包 {r.built} 个月，{r.unchanged} 个月无变化"]
        lines += [f"归档错误：{e}" for e in r.errors[:5]]
        return lines

    def auto_delete(self, progress: Task, sources: dict[int, PhotoSource]) -> list[str]:
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
            progress(f"正在自动清理 {account['name']} 的 {len(plan)} 张", 0, len(plan))
            r = execute_deletion(
                self.db, self.cfg.backup_root, self.cfg.archive_root, source, account["id"],
                [c.asset_id for c in plan], "auto", progress,
            )
            lines.append(f"{account['name']}：自动删除 {r.deleted} 张，跳过 {r.skipped} 张，失败 {len(r.errors)} 张")
        return lines

    def cycle(self, progress: Task) -> str:
        lines, sources = self.sync(progress)
        lines += self.archive(progress)
        lines += self.auto_delete(progress, sources)
        self.db.set_setting("last_cycle_at", utcnow())
        return "\n".join(lines)

    def manual_delete(self, progress: Task, account_id: int, asset_ids: list[str]) -> str:
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
            # 上一轮还在运行或排队时，submit_cycle 不会重复加入
            if self.due(datetime.now(UTC)):
                self.jobs.submit_cycle("定时运行")
