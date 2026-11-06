import re
import threading

import pytest
from conftest import requires_7zz
from fastapi.testclient import TestClient

from icloud_photo_vault.accounts import normalize_apple_id
from icloud_photo_vault.source import FakeBackend
from icloud_photo_vault.web.app import create_app, library_progress, month_ranges, percent


@pytest.fixture
def client(cfg):
    app = create_app(cfg, backend=FakeBackend(cfg.state_dir, count=40), start_scheduler=False)
    with TestClient(app) as client:
        client.app_state = app.state
        yield client


def add_and_login(client, name="alice"):
    """走一遍添加账号对话框：账号信息、密码、验证码、完成。"""
    r = client.post("/accounts", data={"name": name, "apple_id": f"{name}@example.com", "domain": "cn"})
    assert f"输入 <strong>{name}@example.com</strong> 的密码" in r.text
    account_id = int(re.search(r"/accounts/(\d+)/login", r.text).group(1))
    r = client.post(f"/accounts/{account_id}/login", data={"password": "pw", "flow": "new"})
    assert "输入双重认证验证码" in r.text
    r = client.post(f"/accounts/{account_id}/code", data={"code": "000000", "flow": "new"})
    assert "验证码不正确" in r.text and "输入双重认证验证码" in r.text
    r = client.post(f"/accounts/{account_id}/code", data={"code": "123456", "flow": "new"})
    assert f"已添加 <strong>{name}</strong>" in r.text
    assert "登录成功，已开始同步这个账号" in r.text
    # 登录后自动同步，只下载，不删除
    client.app_state.jobs.wait(30)
    assert client.app_state.jobs.history[0].name == f"同步 {name}"
    assert "检查 40 张" in client.app_state.jobs.history[0].summary
    return account_id


def run_cycle(client):
    r = client.post("/jobs/cycle")
    assert "已开始" in r.text
    client.app_state.jobs.wait(30)
    return client.app_state.jobs.history[0]


def events(client):
    return client.get("/log?view=events").text


def test_pages_render(client):
    for path in ("/", "/accounts/new", "/cleanup", "/log", "/log?view=tasks", "/log?view=deletions", "/settings"):
        assert client.get(path).status_code == 200, path


def test_status_api(client):
    status = client.get("/api/status").json()
    assert status["running"] is False and status["last"] is None and status["task"] is None
    assert status["accounts"] == {"total": 0, "logged_in": 0, "need_login": []}
    assert "还没有账号" in client.get("/statusbar").text
    add_and_login(client)
    status = client.get("/api/status").json()
    assert status["last"]["name"] == "同步 alice" and status["last"]["ok"] is True
    assert status["accounts"]["logged_in"] == 1
    bar = client.get("/statusbar").text
    assert 'data-running="0"' in bar and "上次任务：同步 alice · " in bar and "1 个账号已登录" in bar


def test_statusbar_shows_task_once(client):
    add_and_login(client)
    jobs = client.app_state.jobs
    release = threading.Event()

    def task(progress):
        progress.syncing = {"index": 1, "total": 2, "name": "alice"}
        progress("已检查 50 / 200 张，下载 60 个文件", 50, 200)
        release.wait(10)
        return ""

    running = jobs.submit("同步 alice", task).task
    try:
        for _ in range(100):
            if running.meter:
                break
            threading.Event().wait(0.01)
        bar = client.get("/statusbar").text
        # 任务名里已经有账号名，不再重复
        assert bar.count("alice") == 1
        assert "已检查 50 / 200 张" in bar and "25%" in bar
        running.name = "完整运行"
        running.phase = "同步"
        running("已检查 50 / 200 张", 50, 200, note="新下载 3 个文件，其余已在 NAS 上")
        bar = client.get("/statusbar").text
        assert "同步 alice（第 1/2 个账号）" in bar
        # 检查进度和新下载的数量分开显示
        assert re.search(r"已检查 50 / 200 张</span>.*25%</span>.*新下载 3 个文件", bar, re.S)
    finally:
        release.set()
        jobs.wait(10)


def test_statusbar_reports_failure_and_login(client):
    client.post("/accounts", data={"name": "alice", "apple_id": "alice@example.com"})
    client.app_state.jobs.submit("归档", lambda p: 1 / 0)
    client.app_state.jobs.wait(10)
    bar = client.get("/statusbar").text
    assert "归档失败" in bar and "alice 需要登录" in bar


def test_static_files(client):
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_overview_shows_metrics(client):
    add_and_login(client)
    page = client.get("/").text
    assert 'http-equiv="refresh"' not in page
    for text in ("备份进度", "可释放空间", "数量统计", "运行状况", "归档密码"):
        assert text in page
    assert "未设置，归档和自动清理不会运行" in page
    with client.app_state.db.connect() as conn:
        total, videos = conn.execute("SELECT icloud_total, icloud_videos FROM accounts").fetchone()
    assert total == 40
    assert f"照片 {total - videos} · 视频 {videos}" in page
    assert "全部已备份" in page
    # 数量统计按年份分开显示照片和视频
    assert 'class="photo"' in page and 'class="video"' in page
    rows = re.findall(r'<span class="yvalue">照片 (\d+) · 视频 (\d+)', page)
    assert sum(int(p) for p, _ in rows) == total - videos and sum(int(v) for _, v in rows) == videos


def test_library_progress_uses_icloud_count():
    stats = {"in_icloud": 100, "videos": 8, "backed_up": 90, "two_copies": 50}
    counted = {"icloud_total": 300, "icloud_videos": 20}
    p = library_progress([counted], [stats])
    assert (p["total"], p["photos"], p["videos"], p["missing"], p["known"]) == (300, 280, 20, 210, True)
    # 还没查到总数时，退回到已经列出的数量
    p = library_progress([{"icloud_total": None, "icloud_videos": None}], [stats])
    assert (p["total"], p["videos"], p["missing"], p["known"]) == (100, 8, 10, False)


def test_add_account_rejects_bad_name_and_keeps_input(client):
    r = client.post("/accounts", data={"name": "../x", "apple_id": "a@b.c"})
    assert "不能含空格" in r.text
    assert 'value="a@b.c"' in r.text


@pytest.mark.parametrize(
    ("raw", "domain", "expected"),
    [
        ("13812345678", "cn", "8613812345678"),
        (" +86 138-1234-5678 ", "cn", "8613812345678"),
        ("0086 13812345678", "cn", "8613812345678"),
        ("8613812345678", "cn", "8613812345678"),
        ("＋８６ １３８１２３４５６７８", "cn", "8613812345678"),
        (" Name@Example.com ", "cn", "Name@Example.com"),
        ("12125550123", "cn", "12125550123"),
        ("13812345678", "com", "13812345678"),
    ],
)
def test_normalize_apple_id(raw, domain, expected):
    assert normalize_apple_id(raw, domain) == expected


def test_add_account_normalizes_phone_number(client):
    r = client.post("/accounts", data={"name": "bob", "apple_id": "138 1234 5678", "domain": "cn"})
    assert "输入 <strong>8613812345678</strong> 的密码" in r.text
    # 同一个号码换一种写法，也能认出是重复添加
    r = client.post("/accounts", data={"name": "bob2", "apple_id": "+86 13812345678", "domain": "cn"})
    assert "Apple ID 已经存在" in r.text


def test_discard_half_added_account(client):
    client.post("/accounts", data={"name": "alice", "apple_id": "alice@example.com"})
    r = client.post("/accounts/1/discard")
    assert "已取消添加 alice" in r.text
    assert client.get("/accounts/1").status_code == 404
    assert "取消添加" in events(client)


def test_discard_refuses_logged_in_account(client):
    add_and_login(client)
    r = client.post("/accounts/1/discard")
    assert "不能取消添加" in r.text
    assert client.get("/accounts/1").status_code == 200


def test_cycle_without_login_skips_account(client):
    client.post("/accounts", data={"name": "alice", "apple_id": "alice@example.com"})
    job = run_cycle(client)
    assert "需要重新登录" in job.summary


@requires_7zz
def test_auto_cleanup_flow(client):
    add_and_login(client)
    r = client.post("/settings/password", data={"password": "correct horse battery", "again": "correct horse battery"})
    assert "归档密码已设置" in r.text
    # 有待归档的月份时，运行状况里可以直接归档
    assert '<button type="submit" class="small">立即归档</button>' in client.get("/").text

    job = run_cycle(client)
    assert job.ok, job.summary
    assert "v1" in client.get("/accounts/1").text

    # 默认只生成报告，不删除
    review = client.get("/cleanup?account=1&review=auto").text
    assert "当前只生成报告，不会删除" in review
    planned = int(re.search(r"且有两份副本 · (\d+) 张", review).group(1))
    assert planned > 0
    with client.app_state.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM deletions").fetchone()[0] == 0

    # 开启后，下一轮自动清理，并记下规则变化
    client.post("/cleanup/rule", data={"mode": "on", "years": "2", "limit": "500", "account": "1"})
    assert "模式 只生成报告 改为 自动清理" in events(client)
    job = run_cycle(client)
    assert f"自动删除 {planned} 张" in job.summary
    assert client.get("/log?view=deletions").text.count("自动清理") >= planned


@requires_7zz
def test_manual_cleanup_requires_confirmation_and_matching_count(client):
    add_and_login(client)
    client.post("/settings/password", data={"password": "correct horse battery", "again": "correct horse battery"})
    run_cycle(client)
    page = client.get("/cleanup?account=1").text
    months = [m for m, n in re.findall(r'name="months" value="([^"]+)" data-count="(\d+)"', page) if int(n)][:3]
    assert months
    query = "&".join(f"months={m}" for m in months)
    review = client.get(f"/cleanup?account=1&{query}&review=manual").text
    assert "确认从 iCloud 删除" in review
    deletable = int(re.search(r'name="expected" value="(\d+)"', review).group(1))
    assert deletable > 0
    form = {"account": "1", "months": months, "expected": str(deletable)}

    r = client.post("/cleanup/delete", data=form)
    assert "请勾选确认框" in r.text and "确认从 iCloud 删除" in r.text

    r = client.post("/cleanup/delete", data={**form, "expected": str(deletable + 1), "confirm": "yes"})
    assert "清单已经变化" in r.text

    r = client.post("/cleanup/delete", data={**form, "confirm": "yes"})
    assert f"已开始删除 {deletable} 张照片" in r.text
    client.app_state.jobs.wait(30)
    assert f"人工确认删除 {deletable} 张" in client.app_state.jobs.history[0].summary
    assert f"从 iCloud 删除 {deletable} 张" in events(client)
    review = client.get(f"/cleanup?account=1&{query}&review=manual").text
    assert 'name="expected"' not in review


def test_cleanup_rejects_bad_month(client):
    add_and_login(client)
    assert client.get("/cleanup?account=1&months=2020-1&review=manual").status_code == 400


def test_month_ranges():
    months = ["2020-11", "2020-12", "2021-01", "2021-03", "unknown"]
    assert month_ranges(months) == "2020-11 至 2021-01、2021-03、没有拍摄日期"


def test_settings_changes_are_recorded_with_actor(client):
    client.post("/settings", data={"interval": "12", "volume_mb": "3900"}, headers={"X-Trim-Username": "clow"})
    log = events(client)
    assert "运行间隔 24 小时改为 12 小时" in log
    assert '<td class="muted">clow</td>' in log


@pytest.fixture
def gateway_client(tmp_path):
    from icloud_photo_vault.config import Config

    cfg = Config.from_env(
        {
            "VAULT_DATA_DIR": str(tmp_path / "gw"),
            "VAULT_SOURCE": "fake",
            "VAULT_BASE_PATH": "/app/icloud-photo-vault",
            "VAULT_REQUIRE_ADMIN": "1",
        }
    )
    app = create_app(cfg, backend=FakeBackend(cfg.state_dir, count=5), start_scheduler=False)
    with TestClient(app) as client:
        yield client


ADMIN = {"X-Trim-Isadmin": "true", "X-Trim-Username": "clow"}


def test_gateway_requires_admin(gateway_client):
    assert gateway_client.get("/app/icloud-photo-vault/").status_code == 403
    assert gateway_client.get("/app/icloud-photo-vault/", headers={"X-Trim-Isadmin": "false"}).status_code == 403
    assert gateway_client.get("/app/icloud-photo-vault/", headers=ADMIN).status_code == 200


def test_gateway_prefix_in_links_and_redirects(gateway_client):
    r = gateway_client.get("/app/icloud-photo-vault", headers=ADMIN, follow_redirects=False)
    assert r.headers["location"] == "/app/icloud-photo-vault/"
    page = gateway_client.get("/app/icloud-photo-vault/", headers=ADMIN).text
    assert 'href="/app/icloud-photo-vault/cleanup"' in page
    assert 'href="/app/icloud-photo-vault/accounts/new"' in page
    assert 'href="/cleanup"' not in page
    r = gateway_client.post(
        "/app/icloud-photo-vault/accounts", data={"name": "alice", "apple_id": "a@b.c"}, headers=ADMIN,
        follow_redirects=False,
    )
    assert r.headers["location"] == "/app/icloud-photo-vault/accounts/new?step=password&id=1"
    assert gateway_client.get("/cleanup", headers=ADMIN).status_code == 404


def test_rename_account(client, cfg):
    add_and_login(client)
    r = client.post("/accounts/1/rename", data={"name": "a/b"})
    assert "不能含空格" in r.text
    r = client.post("/accounts/1/rename", data={"name": "小红"})
    assert "已把 alice 改名为 小红" in r.text
    assert (cfg.backup_root / "小红").is_dir() and not (cfg.backup_root / "alice").exists()
    assert "<h1>小红</h1>" in r.text
    assert "alice 改为 小红" in events(client)


def test_rename_refused_while_account_in_use(client):
    add_and_login(client)
    add_and_login(client, "bob")
    jobs = client.app_state.jobs
    release = threading.Event()
    # 只用到 bob 的重任务不影响给 alice 改名
    jobs.submit("占位", lambda p: release.wait(10) and "", accounts=frozenset({2}))
    try:
        r = client.post("/accounts/1/rename", data={"name": "小红"})
        assert "已把 alice 改名为 小红" in r.text
        # 排队中的完整运行会用到所有账号
        assert "已加入队列" in client.post("/jobs/cycle").text
        r = client.post("/accounts/1/rename", data={"name": "小绿"})
        assert "有任务正在使用或等待使用这个账号" in r.text
    finally:
        release.set()
        jobs.wait(30)


def test_queue_runs_after_current_task_and_dedupes(client):
    add_and_login(client)
    jobs = client.app_state.jobs
    release = threading.Event()
    jobs.submit("占位", lambda p: release.wait(10) and "", key="hold")
    try:
        assert "排在第 1 位" in client.post("/jobs/sync/1").text
        assert "“同步 alice”已经在排队" in client.post("/jobs/sync/1").text
        assert "排在第 2 位" in client.post("/jobs/archive").text
        bar = client.get("/statusbar").text
        assert "2 个在排队" in bar and 'data-running="1"' in bar
        page = client.get("/log?view=tasks").text
        assert page.count(">排队中</span>") == 2 and "排在第 2 位" in page
        archive = jobs.snapshot()[1][1]
        r = client.post(f"/jobs/{archive.id}/cancel")
        assert "已取消“归档”" in r.text
        assert "已经开始运行或已经结束" in client.post(f"/jobs/{archive.id}/cancel").text
        assert "取消了排队中的任务：归档" in events(client)
        assert "同步 alice，已加入队列" in events(client)
    finally:
        release.set()
        assert jobs.wait(30)
    names = [t.name for t in jobs.history]
    assert names[:3] == ["同步 alice", "占位", "归档"]
    assert [t.state for t in jobs.history[:3]] == ["done", "done", "cancelled"]
    bar = client.get("/statusbar").text
    # 取消的任务不算“上次任务”
    assert "上次任务：同步 alice" in bar


def test_manual_delete_runs_beside_other_account_sync(client):
    add_and_login(client)
    run_cycle(client)
    jobs = client.app_state.jobs
    release = threading.Event()
    # 正在同步另一个账号，不影响删除 alice 的照片
    jobs.submit("同步 bob", lambda p: release.wait(10) and "", key="sync:2", accounts=frozenset({2}))
    try:
        page = client.get("/cleanup?account=1").text
        month = next(m for m, n in re.findall(r'name="months" value="([^"]+)" data-count="(\d+)"', page) if int(n) and m != "unknown")
        review = client.get(f"/cleanup?account=1&review=manual&months={month}").text
        expected = int(re.search(r'name="expected" value="(\d+)"', review).group(1))
        form = {"account": 1, "expected": expected, "months": month, "confirm": "yes"}
        r = client.post("/cleanup/delete", data=form)
        assert f"已开始删除 {expected} 张照片" in r.text
        for _ in range(500):
            if jobs.history and jobs.history[0].name == "清理 alice":
                break
            threading.Event().wait(0.01)
        assert jobs.history[0].name == "清理 alice"
        # 同步 alice 和正在运行的同步 bob 都是重任务，要排队
        assert "排在第 1 位" in client.post("/jobs/sync/1").text
    finally:
        release.set()
        assert jobs.wait(30)


def test_cleanup_page_does_not_offer_undated_photos(client):
    add_and_login(client)
    stats = client.app_state.stats
    rows = stats.get(1, "")
    undated = next(r for r in rows if r["month"] == "unknown")
    assert undated["backed_up"] > 0
    page = client.get("/cleanup?account=1").text
    tag = re.search(r'<input type="checkbox" name="months" value="unknown"[^>]*>', page).group(0)
    assert "disabled" in tag
    assert "没有拍摄日期，不能删除" in page
    dated = sum(r["backed_up"] for r in rows if r["month"] != "unknown")
    assert f"{dated} 张已备份到 NAS" in page


def test_percent_never_rounds_up_to_full():
    assert (percent(78871, 78893), percent(5, 5), percent(1, 3), percent(7, 10), percent(0, 0)) == (
        "99.9%", "100.0%", "33.3%", "70.0%", "—"
    )


def test_next_run_waits_for_current_cycle(client):
    jobs = client.app_state.jobs
    release = threading.Event()
    jobs.submit("完整运行", lambda p: release.wait(10) and "", key="cycle")
    try:
        assert "下次自动运行 本轮结束 24 小时后" in client.get("/statusbar").text
    finally:
        release.set()
        assert jobs.wait(10)
    assert "下次自动运行 即将开始" in client.get("/statusbar").text
