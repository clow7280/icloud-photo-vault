"""网页界面。"""

import re
import shutil
import sqlite3
from importlib.metadata import version as package_version
from urllib.parse import urlencode
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..accounts import normalize_apple_id, rename_account
from ..archive import ArchiveError, read_password, write_password
from ..config import Config
from ..db import Database, utcnow
from ..backup import month_key
from ..deletion import auto_delete_plan, cutoff_month, manual_delete_preview, years_before
from ..jobs import Jobs, Scheduler, Submission, Task
from ..source import Backend, FakeBackend, SourceError
from ..stats import StatsCache, totals, year_stats

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
STATIC_DIR = Path(__file__).parent / "static"
NAV = {"index.html": "overview", "cleanup.html": "cleanup", "log.html": "log", "settings.html": "settings"}
AUTO_MODES = {"off": "关闭", "report": "只生成报告", "on": "自动清理"}
LOG_VIEWS = {"events": "操作记录", "tasks": "任务", "deletions": "删除记录"}
TASK_STATES = {"queued": "排队中", "running": "运行中", "done": "完成", "failed": "失败", "cancelled": "已取消"}
MONTH = re.compile(r"^(\d{4}-\d{2}|unknown)$")
ACCOUNT_NAME = re.compile(r"^[^/\\.\s][^/\\\s]{0,31}$")
ACCOUNT_NAME_RULE = "账号名称用作目录名，不能含空格、斜杠，不能以点开头，最长 32 个字符"
PREVIEW_ROWS = 300
VERSION = package_version("icloud-photo-vault")


def month_ranges(months: list[str]) -> str:
    """把月份列表写成紧凑的区间，例如 2020-01 至 2020-03、2021-05。"""
    dated = sorted(m for m in months if m != "unknown")
    parts: list[list[str]] = []
    for m in dated:
        year, month = int(m[:4]), int(m[5:])
        if parts:
            ly, lm = int(parts[-1][-1][:4]), int(parts[-1][-1][5:])
            if (year, month) == ((ly, lm + 1) if lm < 12 else (ly + 1, 1)):
                parts[-1].append(m)
                continue
        parts.append([m])
    text = [p[0] if len(p) == 1 else f"{p[0]} 至 {p[-1]}" for p in parts]
    if "unknown" in months:
        text.append("没有拍摄日期")
    return "、".join(text)


def library_progress(accounts: list, stats: list[dict]) -> dict:
    """备份进度以同步开始时从 iCloud 查到的总数为分母；还没查到时退回到已经列出的数量。"""
    total = videos = backed = two = listed = 0
    known = bool(accounts)
    for account, t in zip(accounts, stats):
        listed += t["in_icloud"]
        backed += t["backed_up"]
        two += t["two_copies"]
        if account["icloud_total"] is None:
            known = False
            total += t["in_icloud"]
            videos += t["videos"]
        else:
            total += account["icloud_total"]
            videos += account["icloud_videos"] or 0
    return {
        "total": total,
        "videos": videos,
        "photos": total - videos,
        "backed": min(backed, total),
        "two": min(two, total),
        "missing": max(total - backed, 0),
        "listed": listed,
        "known": known,
    }


def human_bytes(value: int | None) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def make_local_time(cfg: Config):
    def local_time(value: str | None) -> str:
        if not value:
            return "—"
        return datetime.fromisoformat(value).astimezone(cfg.timezone).strftime("%Y-%m-%d %H:%M")

    return local_time


def default_backend(cfg: Config) -> Backend:
    if cfg.source == "fake":
        return FakeBackend(cfg.state_dir)
    from ..icloud import ICloudBackend

    return ICloudBackend(cfg.sessions_dir)


def create_app(cfg: Config, backend: Backend | None = None, start_scheduler: bool = True) -> FastAPI:
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    backend = backend or default_backend(cfg)
    stats = StatsCache(db.connect)
    jobs = Jobs(db, cfg, backend, on_finish=stats.invalidate)
    scheduler = Scheduler(jobs)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_scheduler:
            # 和定时任务一起启动：先在后台把统计算好，第一次打开页面不用等
            with db.connect() as conn:
                stats.warm([a["id"] for a in all_accounts(conn)], cutoff_iso(datetime.now(UTC)))
            scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(title="iCloud 照片保管库", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.jobs, app.state.cfg, app.state.stats = db, jobs, cfg, stats
    router = APIRouter()
    base = cfg.base_path

    if cfg.require_admin:
        # 统一网关确认用户已登录 NAS 后，才会附带这些请求头转发过来
        @app.middleware("http")
        async def require_admin(request: Request, call_next):
            if request.headers.get("x-trim-isadmin") != "true":
                return HTMLResponse("只有 NAS 管理员可以使用 iCloud 照片保管库。", status_code=403)
            return await call_next(request)
    TEMPLATES.env.filters["bytes"] = human_bytes
    TEMPLATES.env.filters["local"] = make_local_time(cfg)
    TEMPLATES.env.filters["month_ranges"] = month_ranges

    local_time = make_local_time(cfg)

    def describe(task: Task) -> dict:
        syncing, meter = task.syncing, task.meter
        # 任务现在在做什么：完整运行的哪一步、哪个账号。任务名里已经有账号名时（例如“同步 小红”）不再重复
        context = [task.phase] if task.phase else []
        if syncing and syncing["name"] not in task.name:
            of = f"（第 {syncing['index']}/{syncing['total']} 个账号）" if syncing["total"] > 1 else ""
            context.append(syncing["name"] + of)
        return {
            "name": task.name,
            "context": " ".join(context),
            "step": task.step,
            "note": task.note,
            "done": meter[0] if meter else None,
            "total": meter[1] if meter else None,
            "percent": min(100, int(100 * meter[0] / meter[1])) if meter else None,
        }

    def last_finished(history: list[Task]) -> Task | None:
        """最近一个运行过的任务，排队时被取消的不算。"""
        return next((t for t in history if t.state != "cancelled"), None)

    def status_payload() -> dict:
        """底部状态栏的内容，页面渲染和轮询共用。"""
        running, queued, history = jobs.snapshot()
        # 同时运行多个任务时，主要显示重任务，其他的只显示数量
        main = next((t for t in running if t.heavy), running[0] if running else None)
        more = []
        if len(running) > 1:
            more.append(f"另有 {len(running) - 1} 个在运行")
        if queued:
            more.append(f"{len(queued)} 个在排队")
        last = last_finished(history)
        with db.connect() as conn:
            accounts = conn.execute("SELECT id, name, auth_state FROM accounts ORDER BY id").fetchall()
        need_login = [{"id": a["id"], "name": a["name"]} for a in accounts if a["auth_state"] != "ok"]
        last_cycle = db.get_setting("last_cycle_at")
        hours = db.get_int("sync_interval_hours")
        next_cycle = None
        if last_cycle:
            due = datetime.fromisoformat(last_cycle) + timedelta(hours=hours)
            next_cycle = local_time(due.isoformat()) if due > datetime.now(UTC) else None
        # 完整运行正在运行或排队时，下一轮从这一轮结束时算起
        if any(t.key == "cycle" for t in running + queued):
            next_text = f"本轮结束 {hours} 小时后"
        else:
            next_text = next_cycle or "即将开始"
        return {
            "running": bool(running or queued),
            "task": describe(main) if main else None,
            "more": "，".join(more),
            "queued": len(queued),
            "finished": jobs.finished_count,
            "last": None
            if last is None
            else {"name": last.name, "ok": last.ok, "summary": last.summary, "finished_at": local_time(last.finished_at)},
            "accounts": {"total": len(accounts), "logged_in": len(accounts) - len(need_login), "need_login": need_login},
            "last_cycle": local_time(last_cycle),
            "next_cycle": next_cycle,
            "next_text": next_text,
        }

    def submitted(sub: Submission, started: str) -> str:
        """提交任务后给用户看的说明。"""
        if sub.outcome == "started":
            return started
        if sub.outcome == "queued":
            return f"已加入队列，排在第 {sub.position} 位，前面的任务结束后自动开始"
        if sub.task.state == "running":
            return f"“{sub.task.name}”已经在运行，不用重复提交"
        return f"“{sub.task.name}”已经在排队，不用重复提交"

    def record_submission(request: Request, sub: Submission, action: str, detail: str, account_id: int | None = None):
        if sub.outcome in ("started", "queued"):
            record(request, action, detail + ("，已加入队列" if sub.outcome == "queued" else ""), account_id)

    def render(request: Request, name: str, **context) -> HTMLResponse:
        context.setdefault("active", NAV.get(name))
        with db.connect() as conn:
            nav_accounts = conn.execute("SELECT id, name, apple_id, auth_state FROM accounts ORDER BY id").fetchall()
        context.update(
            jobs=jobs,
            fake=cfg.source == "fake",
            base=base,
            nav_accounts=nav_accounts,
            status=status_payload(),
            version=VERSION,
        )
        context.setdefault("message", request.query_params.get("msg"))
        context.setdefault("error", request.query_params.get("err"))
        context.setdefault("dialog", None)
        return TEMPLATES.TemplateResponse(request, name, context)

    def back(url: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
        query = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
        if query:
            url += ("&" if "?" in url else "?") + query
        return RedirectResponse(f"{base}{url}", status_code=303)

    def get_account(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "账号不存在")
        return row

    def actor(request: Request) -> str | None:
        """统一网关转发时附带的 NAS 用户名，本地运行时没有。"""
        return request.headers.get("x-trim-username")

    def record(request: Request, action: str, detail: str, account_id: int | None = None) -> None:
        db.add_event(actor(request), action, detail, account_id)

    def all_accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()

    def cutoff_iso(now: datetime) -> str:
        """统计用的自动清理分界时间。取整到小时，统计缓存才不会每秒都过期。"""
        hour = now.replace(minute=0, second=0, microsecond=0)
        return years_before(hour, db.get_int("auto_delete_years")).isoformat(timespec="seconds")

    # ---- 总览 ----

    def disk(path: Path) -> dict | None:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return None
        return {"free": usage.free, "total": usage.total, "free_ratio": usage.free / usage.total if usage.total else 0}

    def health_items(
        accounts: list[sqlite3.Row], pending_archive: int, archive: sqlite3.Row, nas_bytes: int
    ) -> list[dict]:
        """总览右侧的运行状况。state：ok 正常，warn 需要留意，bad 需要处理，info 只是说明。"""
        items = []
        password_set = read_password(cfg) is not None
        items.append({"label": "归档密码", "state": "ok" if password_set else "bad",
                      "value": "已设置" if password_set else "未设置，归档和自动清理不会运行", "href": "/settings"})
        need_login = [a["name"] for a in accounts if a["auth_state"] != "ok"]
        items.append({"label": "iCloud 登录", "state": "bad" if need_login else ("ok" if accounts else "info"),
                      "value": f"{'、'.join(need_login)} 需要登录" if need_login else f"{len(accounts)} 个账号已登录"})
        synced = [a for a in accounts if a["auth_state"] == "ok"]
        errors = [a["name"] for a in synced if a["last_sync_error"]]
        never = [a["name"] for a in synced if not a["last_sync_at"]]
        interval = timedelta(hours=db.get_int("sync_interval_hours"))
        if errors:
            sync_item = {"state": "warn", "value": f"{'、'.join(errors)} 上次同步有问题"}
        elif never:
            sync_item = {"state": "warn", "value": f"{'、'.join(never)} 还没有完成过同步"}
        elif synced:
            oldest = min(datetime.fromisoformat(a["last_sync_at"]) for a in synced)
            late = datetime.now(UTC) - oldest > interval * 2
            sync_item = {"state": "warn" if late else "ok", "value": local_time(oldest.isoformat())}
        else:
            sync_item = {"state": "info", "value": "还没有账号"}
        items.append({"label": "最近同步", **sync_item})
        items.append({"label": "待归档", "state": "warn" if pending_archive else "ok",
                      "value": f"{pending_archive} 个月有新照片还没进归档包" if pending_archive else "全部已归档",
                      "action": ("/jobs/archive", "立即归档") if pending_archive and password_set else None})
        items.append({"label": "NAS 占用", "state": "info", "value": f"备份 {human_bytes(nas_bytes)}"})
        items.append({"label": "归档包", "state": "info",
                      "value": f"{archive['months']} 个月 · {human_bytes(archive['bytes'])}"})
        mode = db.get_setting("auto_delete_mode")
        items.append({"label": "自动清理", "state": "info", "href": "/cleanup",
                      "value": f"{AUTO_MODES[mode]} · 保留最近 {db.get_int('auto_delete_years')} 年"})
        last = last_finished(jobs.history)
        if last:
            items.append({"label": "最近任务", "state": "ok" if last.ok else "bad", "href": "/log?view=tasks",
                          "value": f"{last.name}{'完成' if last.ok else '失败'} · {local_time(last.finished_at)}"})
        for label, path in (("备份卷剩余", cfg.backup_root), ("归档卷剩余", cfg.archive_root)):
            usage = disk(path)
            if usage:
                state = "bad" if usage["free_ratio"] < 0.05 else "warn" if usage["free_ratio"] < 0.1 else "ok"
                items.append({"label": label, "state": state,
                              "value": f"{human_bytes(usage['free'])}，共 {human_bytes(usage['total'])}"})
        return items

    def overview_context() -> dict:
        now = datetime.now(UTC)
        current_month = month_key(now, cfg.timezone)
        cutoff = cutoff_iso(now)
        with db.connect() as conn:
            accounts = all_accounts(conn)
            per_account = [(a, stats.get(a["id"], cutoff)) for a in accounts]
            archive = conn.execute(
                "SELECT COUNT(*) AS months, COALESCE(SUM(total_size), 0) AS bytes FROM archives"
            ).fetchone()
        rows = [r for _, months in per_account for r in months]
        # 已经结束的月份里还有照片没进归档包，下一轮归档会处理
        pending_archive = sum(
            1 for r in rows if (r["month"] == "unknown" or r["month"] < current_month) and r["backed_up"] > r["two_copies"]
        )
        summary = [(a, totals(months)) for a, months in per_account]
        return {
            "summary": [(a, t, library_progress([a], [t])) for a, t in summary],
            "library": library_progress([a for a, _ in summary], [t for _, t in summary]),
            "grand": totals(rows),
            "years": year_stats(rows),
            "health": health_items(accounts, pending_archive, archive, totals(rows)["nas_bytes"]),
            "archive": archive,
            "auto_years": db.get_int("auto_delete_years"),
            "cutoff_month": cutoff_month(now, db.get_int("auto_delete_years"), cfg.timezone),
            "last_cycle": db.get_setting("last_cycle_at"),
        }

    @router.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return render(request, "index.html", **overview_context())

    # ---- 添加账号：在总览上方打开的对话框，依次填写账号、密码、验证码 ----

    def add_dialog(request: Request, step: str, account: sqlite3.Row | None = None, **extra) -> HTMLResponse:
        return render(
            request, "index.html", dialog="dialogs/add_account.html", step=step, new_account=account,
            **extra, **overview_context(),
        )

    @router.get("/accounts/new", response_class=HTMLResponse)
    def new_account(request: Request, step: str = "info", id: int | None = None):
        if step == "info" or id is None:
            return add_dialog(request, "info", form={})
        with db.connect() as conn:
            account = get_account(conn, id)
        if step not in ("password", "code", "done") or (step != "done" and account["auth_state"] == "ok"):
            step = "done"
        return add_dialog(request, step, account)

    @router.post("/accounts", response_class=HTMLResponse)
    def add_account(
        request: Request,
        name: Annotated[str, Form()],
        apple_id: Annotated[str, Form()],
        domain: Annotated[str, Form()] = "cn",
    ):
        name, apple_id = name.strip(), normalize_apple_id(apple_id, domain)
        form = {"name": name, "apple_id": apple_id, "domain": domain}
        # 出错时直接重新显示对话框，不把 Apple ID 放进网址
        if not ACCOUNT_NAME.match(name):
            return add_dialog(request, "info", form=form, error=ACCOUNT_NAME_RULE)
        if domain not in ("cn", "com"):
            return add_dialog(request, "info", form=form, error="区域只能是 cn 或 com")
        try:
            with db.connect() as conn:
                account_id = conn.execute(
                    "INSERT INTO accounts (name, apple_id, domain, created_at) VALUES (?, ?, ?, ?)",
                    (name, apple_id, domain, utcnow()),
                ).lastrowid
        except sqlite3.IntegrityError:
            return add_dialog(request, "info", form=form, error="账号名称或 Apple ID 已经存在")
        record(request, "添加账号", f"{name}（{apple_id}）", account_id)
        return back(f"/accounts/new?step=password&id={account_id}")

    @router.post("/accounts/{account_id}/discard")
    def discard_account(request: Request, account_id: int):
        """取消添加：只允许删除从未登录、也没有同步过照片的账号。"""
        with db.connect() as conn:
            account = get_account(conn, account_id)
            used = conn.execute("SELECT 1 FROM assets WHERE account_id = ? LIMIT 1", (account_id,)).fetchone()
            if account["auth_state"] == "ok" or used:
                return back(f"/accounts/{account_id}", err="这个账号已经登录或同步过，不能取消添加")
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        shutil.rmtree(cfg.sessions_dir / str(account_id), ignore_errors=True)
        record(request, "取消添加", account["name"])
        return back("/", msg=f"已取消添加 {account['name']}")

    @router.get("/accounts/{account_id}", response_class=HTMLResponse)
    def account_page(request: Request, account_id: int):
        with db.connect() as conn:
            account = get_account(conn, account_id)
        months = stats.get(account_id, cutoff_iso(datetime.now(UTC)))
        return render(
            request,
            "account.html",
            active=f"account-{account_id}",
            account=account,
            months=months,
            total=totals(months),
            library=library_progress([account], [totals(months)]),
            awaiting_code=request.query_params.get("code") == "1",
        )

    @router.post("/accounts/{account_id}/login")
    def login(request: Request, account_id: int, password: Annotated[str, Form()], flow: Annotated[str, Form()] = ""):
        with db.connect() as conn:
            account = get_account(conn, account_id)
        new = flow == "new"
        url = f"/accounts/{account_id}"
        try:
            state = backend.start_login(account_id, account["domain"], account["apple_id"], password)
        except SourceError as e:
            return back(f"/accounts/new?step=password&id={account_id}" if new else url, err=str(e))
        if state == "need_code":
            sent = "密码正确，验证码已发送到这个 Apple ID 的受信任设备"
            return back(f"/accounts/new?step=code&id={account_id}" if new else f"{url}?code=1", msg=sent)
        return logged_in(request, account_id, new)

    @router.post("/accounts/{account_id}/code")
    def submit_code(request: Request, account_id: int, code: Annotated[str, Form()], flow: Annotated[str, Form()] = ""):
        new = flow == "new"
        retry = f"/accounts/new?step=code&id={account_id}" if new else f"/accounts/{account_id}?code=1"
        try:
            ok = backend.submit_code(account_id, code)
        except SourceError as e:
            return back(f"/accounts/new?step=password&id={account_id}" if new else f"/accounts/{account_id}", err=str(e))
        if not ok:
            return back(retry, err="验证码不正确")
        return logged_in(request, account_id, new)

    def logged_in(request: Request, account_id: int, new: bool) -> RedirectResponse:
        with db.connect() as conn:
            conn.execute("UPDATE accounts SET auth_state = 'ok', last_sync_error = NULL WHERE id = ?", (account_id,))
            account = get_account(conn, account_id)
        record(request, "登录", f"{account['name']} 登录 iCloud 成功", account_id)
        msg = after_login(account)
        return back(f"/accounts/new?step=done&id={account_id}" if new else f"/accounts/{account_id}", msg=msg)

    def after_login(account: sqlite3.Row) -> str:
        """登录成功后马上同步这个账号，只下载，不删除。"""
        account_id = account["id"]
        sub = jobs.submit_sync(account_id, account["name"])
        if sub.outcome == "started":
            return "登录成功，已开始同步这个账号"
        if sub.outcome == "queued":
            return "登录成功，前面的任务结束后会自动同步这个账号"
        return "登录成功，这个账号的同步已经在运行或排队"

    @router.post("/accounts/{account_id}/rename")
    def rename(request: Request, account_id: int, name: Annotated[str, Form()]):
        url = f"/accounts/{account_id}"
        name = name.strip()
        if not ACCOUNT_NAME.match(name):
            return back(url, err=ACCOUNT_NAME_RULE)
        with db.connect() as conn:
            old_name = get_account(conn, account_id)["name"]
        # 作为任务运行，避免和同步、归档、删除同时操作这个账号的目录
        outcome: dict[str, str] = {}

        def task(_progress) -> str:
            try:
                outcome["msg"] = rename_account(db, cfg, account_id, name)
            except Exception as e:
                outcome["err"] = f"改名失败：{e}"
                raise
            return outcome["msg"]

        sub = jobs.submit_rename(account_id, name, task)
        if sub.outcome != "started":
            return back(url, err="有任务正在使用或等待使用这个账号，请等它们结束后再改名")
        sub.task.finished.wait(60)
        if "msg" in outcome:
            if old_name != name:
                record(request, "改名", f"{old_name} 改为 {name}", account_id)
            return back(url, msg=outcome["msg"])
        return back(url, err=outcome.get("err", "改名还没完成，请稍后刷新"))

    # ---- 任务 ----

    @router.get("/api/status")
    def status():
        return JSONResponse(status_payload())

    @router.get("/statusbar", response_class=HTMLResponse)
    def statusbar(request: Request):
        """状态栏片段，页面轮询后整块替换。"""
        return TEMPLATES.TemplateResponse(
            request, "_statusbar.html", {"status": status_payload(), "base": base, "fake": cfg.source == "fake"}
        )

    @router.post("/jobs/cycle")
    def run_cycle(request: Request):
        sub = jobs.submit_cycle()
        record_submission(request, sub, "启动任务", "完整运行：同步、归档、自动清理")
        return back("/", msg=submitted(sub, "已开始：同步、归档、自动清理"))

    @router.post("/jobs/sync/{account_id}")
    def run_sync(request: Request, account_id: int):
        with db.connect() as conn:
            account = get_account(conn, account_id)
        sub = jobs.submit_sync(account_id, account["name"])
        record_submission(request, sub, "启动任务", f"同步 {account['name']}", account_id)
        return back(f"/accounts/{account_id}", msg=submitted(sub, "已开始同步"))

    @router.post("/jobs/archive")
    def run_archive_job(request: Request):
        sub = jobs.submit_archive()
        record_submission(request, sub, "启动任务", "归档")
        return back("/", msg=submitted(sub, "已开始归档"))

    @router.post("/jobs/{task_id}/cancel")
    def cancel_job(request: Request, task_id: int):
        url = "/log?view=tasks"
        task = jobs.cancel(task_id)
        if task is None:
            return back(url, err="这个任务已经开始运行或已经结束，不能取消")
        record(request, "取消任务", f"取消了排队中的任务：{task.name}", next(iter(task.accounts or []), None))
        return back(url, msg=f"已取消“{task.name}”")

    # ---- 空间清理：人工选择月份删除，和按规则自动清理，共用一张月份表 ----

    def cleanup_url(account_id: int, months: list[str] | None = None, review: str = "") -> str:
        query = [("account", account_id), *[("months", m) for m in months or []]]
        if review:
            query.append(("review", review))
        return f"/cleanup?{urlencode(query)}"

    def check_months(months: list[str]) -> list[str]:
        for m in months:
            if not MONTH.match(m):
                raise HTTPException(400, f"月份格式不对：{m}")
        return sorted(set(months), reverse=True)

    @router.get("/cleanup", response_class=HTMLResponse)
    def cleanup_page(
        request: Request,
        account: int | None = None,
        review: str = "",
        months: Annotated[list[str], Query()] = [],
    ):
        months = check_months(months)
        now = datetime.now(UTC)
        years, limit = db.get_int("auto_delete_years"), db.get_int("auto_delete_max_per_run")
        with db.connect() as conn:
            accounts = all_accounts(conn)
            current = next((a for a in accounts if a["id"] == account), accounts[0] if accounts else None)
        rows = stats.get(current["id"], cutoff_iso(now)) if current else []
        context: dict = {
            "accounts": accounts,
            "account": current,
            "months": rows,
            "total": totals(rows),
            # 没有拍摄日期的照片不能删除，不算进可以删除的数量
            "deletable": totals([r for r in rows if r["month"] != "unknown"]),
            "mode": db.get_setting("auto_delete_mode"),
            "modes": AUTO_MODES,
            "years": years,
            "limit": limit,
            "cutoff_month": cutoff_month(now, years, cfg.timezone),
            "selected": set(months),
        }
        if current and review == "manual" and months:
            items = manual_delete_preview(db, cfg.backup_root, cfg.archive_root, current["id"], months)
            deletable = [c for c in items if c.backed_up]
            context.update(
                dialog="dialogs/cleanup_review.html",
                review="manual",
                selected_months=months,
                items=items[:PREVIEW_ROWS],
                item_total=len(items),
                deletable=len(deletable),
                two_copies=sum(1 for c in deletable if c.copies == 2),
                deletable_bytes=sum(c.size for c in deletable),
                preview_rows=PREVIEW_ROWS,
            )
        elif current and review == "auto":
            plan = auto_delete_plan(db, cfg.backup_root, cfg.archive_root, current["id"], now, years, limit)
            context.update(
                dialog="dialogs/cleanup_review.html",
                review="auto",
                items=plan[:PREVIEW_ROWS],
                item_total=len(plan),
                deletable=len(plan),
                two_copies=len(plan),
                deletable_bytes=sum(c.size for c in plan),
                preview_rows=PREVIEW_ROWS,
            )
        return render(request, "cleanup.html", **context)

    @router.post("/cleanup/delete")
    def cleanup_delete(
        request: Request,
        account: Annotated[int, Form()],
        expected: Annotated[int, Form()],
        months: Annotated[list[str], Form()] = [],
        confirm: Annotated[str, Form()] = "",
    ):
        months = check_months(months)
        review = cleanup_url(account, months, "manual")
        if confirm != "yes":
            return back(review, err="请勾选确认框")
        with db.connect() as conn:
            name = get_account(conn, account)["name"]
        items = manual_delete_preview(db, cfg.backup_root, cfg.archive_root, account, months)
        asset_ids = [c.asset_id for c in items if c.backed_up]
        if len(asset_ids) != expected:
            return back(review, err="清单已经变化，请重新核对后再确认")
        if not asset_ids:
            return back(review, err="没有可以删除的照片")
        sub = jobs.submit_manual_delete(account, name, asset_ids)
        detail = f"{name}：从 iCloud 删除 {len(asset_ids)} 张，月份 {month_ranges(months)}"
        record_submission(request, sub, "人工删除", detail, account)
        if sub.outcome == "queued":
            msg = f"删除 {len(asset_ids)} 张照片{submitted(sub, '')}。删除前会逐张重新检查备份"
        else:
            msg = submitted(sub, f"已开始删除 {len(asset_ids)} 张照片，进度见底部状态栏")
        return back(cleanup_url(account), msg=msg)

    @router.post("/cleanup/rule")
    def cleanup_rule(
        request: Request,
        mode: Annotated[str, Form()],
        years: Annotated[int, Form()],
        limit: Annotated[int, Form()],
        account: Annotated[int | None, Form()] = None,
    ):
        url = cleanup_url(account) if account else "/cleanup"
        if mode not in AUTO_MODES:
            return back(url, err="未知的模式")
        if not 1 <= years <= 50:
            return back(url, err="保留年限应在 1 到 50 之间")
        if not 1 <= limit <= 10000:
            return back(url, err="每轮上限应在 1 到 10000 之间")
        old = (db.get_setting("auto_delete_mode"), db.get_int("auto_delete_years"), db.get_int("auto_delete_max_per_run"))
        changes = []
        if old[0] != mode:
            changes.append(f"模式 {AUTO_MODES[old[0]]} 改为 {AUTO_MODES[mode]}")
        if old[1] != years:
            changes.append(f"保留年限 {old[1]} 年改为 {years} 年")
        if old[2] != limit:
            changes.append(f"每轮上限 {old[2]} 张改为 {limit} 张")
        db.set_setting("auto_delete_mode", mode)
        db.set_setting("auto_delete_years", str(years))
        db.set_setting("auto_delete_max_per_run", str(limit))
        stats.invalidate()
        if changes:
            record(request, "清理规则", "，".join(changes))
        return back(url, msg="已保存清理规则" if changes else "规则没有变化")

    # ---- 日志与设置 ----

    @router.get("/log", response_class=HTMLResponse)
    def log_page(request: Request, view: str = "events"):
        if view not in LOG_VIEWS:
            view = "events"
        context: dict = {"view": view, "views": LOG_VIEWS}
        with db.connect() as conn:
            if view == "events":
                context["rows"] = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 500").fetchall()
            elif view == "deletions":
                context["rows"] = conn.execute(
                    """
                    SELECT d.*, a.name AS account_name FROM deletions d
                    JOIN accounts a ON a.id = d.account_id ORDER BY d.id DESC LIMIT 500
                    """
                ).fetchall()
            else:
                running, queued, history = jobs.snapshot()
                context.update(rows=running + queued + history, queued_ids=[t.id for t in queued], states=TASK_STATES)
        return render(request, "log.html", **context)

    @router.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return render(
            request,
            "settings.html",
            cfg=cfg,
            password_set=read_password(cfg) is not None,
            interval=db.get_int("sync_interval_hours"),
            volume_mb=db.get_int("archive_volume_mb"),
        )

    @router.post("/settings")
    def save_settings(request: Request, interval: Annotated[int, Form()], volume_mb: Annotated[int, Form()]):
        if not 1 <= interval <= 24 * 30:
            return back("/settings", err="同步间隔应在 1 到 720 小时之间")
        if not 100 <= volume_mb <= 100_000:
            return back("/settings", err="分卷大小应在 100 MB 到 100000 MB 之间")
        changes = []
        if db.get_int("sync_interval_hours") != interval:
            changes.append(f"运行间隔 {db.get_int('sync_interval_hours')} 小时改为 {interval} 小时")
        if db.get_int("archive_volume_mb") != volume_mb:
            changes.append(f"分卷大小 {db.get_int('archive_volume_mb')} MB 改为 {volume_mb} MB")
        db.set_setting("sync_interval_hours", str(interval))
        db.set_setting("archive_volume_mb", str(volume_mb))
        if changes:
            record(request, "运行设置", "，".join(changes))
        return back("/settings", msg="已保存")

    @router.post("/settings/password")
    def set_password(request: Request, password: Annotated[str, Form()], again: Annotated[str, Form()]):
        if password != again:
            return back("/settings", err="两次输入的密码不一致")
        try:
            write_password(cfg, password)
        except (ArchiveError, FileExistsError) as e:
            return back("/settings", err=str(e))
        record(request, "归档密码", "设置了归档密码")
        return back("/settings", msg="归档密码已设置，请另外妥善保存")

    app.include_router(router, prefix=base)
    app.mount(f"{base}/static", StaticFiles(directory=STATIC_DIR), name="static")
    if base:

        @app.get(base, include_in_schema=False)
        def base_redirect():
            return RedirectResponse(f"{base}/", status_code=307)

    return app
