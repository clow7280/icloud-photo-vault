import threading

import pytest

from icloud_photo_vault.jobs import Jobs
from icloud_photo_vault.source import FakeBackend


@pytest.fixture
def jobs(cfg, db):
    jobs = Jobs(db, cfg, FakeBackend(cfg.state_dir))
    yield jobs
    jobs.wait(10)


class Gate:
    """可以卡住的任务，记录开始顺序。"""

    def __init__(self):
        self.started: list[str] = []
        self.events: dict[str, threading.Event] = {}

    def task(self, label: str):
        self.events[label] = threading.Event()

        def run(_task):
            self.started.append(label)
            self.events[label].wait(10)
            return label

        return run

    def release(self, label: str) -> None:
        self.events[label].set()


def states(jobs):
    running, queued, _ = jobs.snapshot()
    return [t.name for t in running], [t.name for t in queued]


def wait_until(predicate):
    for _ in range(500):
        if predicate():
            return
        threading.Event().wait(0.01)
    raise AssertionError("等待超时")


def test_heavy_tasks_run_one_at_a_time_in_order(jobs):
    gate = Gate()
    assert jobs.submit("A", gate.task("A"), accounts=frozenset({1})).outcome == "started"
    sub = jobs.submit("B", gate.task("B"), accounts=frozenset({2}))
    assert (sub.outcome, sub.position) == ("queued", 1)
    assert jobs.submit("C", gate.task("C")).position == 2
    assert states(jobs) == (["A"], ["B", "C"])
    gate.release("A")
    wait_until(lambda: gate.started == ["A", "B"])
    gate.release("B")
    gate.release("C")
    assert jobs.wait(10)
    assert gate.started == ["A", "B", "C"]
    assert [t.name for t in jobs.history] == ["C", "B", "A"]


def test_light_task_runs_beside_other_account(jobs):
    gate = Gate()
    jobs.submit("sync 1", gate.task("sync 1"), accounts=frozenset({1}))
    sub = jobs.submit("delete 2", gate.task("delete 2"), accounts=frozenset({2}), heavy=False)
    assert sub.outcome == "started"
    # 同一个账号的轻任务要等
    assert jobs.submit("delete 1", gate.task("delete 1"), accounts=frozenset({1}), heavy=False).outcome == "queued"
    # 用到所有账号的任务和谁都冲突
    assert jobs.submit("rename", lambda t: "", accounts=None, heavy=False, queue=False).outcome == "busy"
    gate.release("delete 2")
    wait_until(lambda: states(jobs) == (["sync 1"], ["delete 1"]))
    gate.release("sync 1")
    wait_until(lambda: "delete 1" in gate.started)
    gate.release("delete 1")
    assert jobs.wait(10)


def test_later_task_does_not_overtake_conflicting_queued_task(jobs):
    gate = Gate()
    jobs.submit("sync 1", gate.task("sync 1"), accounts=frozenset({1}))
    jobs.submit("sync 2", gate.task("sync 2"), accounts=frozenset({2}))
    # 账号 2 现在没有任务在运行，但排在前面的同步 2 会用到它
    assert jobs.submit("delete 2", gate.task("delete 2"), accounts=frozenset({2}), heavy=False).outcome == "queued"
    # 和前面的任务都不冲突的轻任务马上运行
    assert jobs.submit("delete 3", gate.task("delete 3"), accounts=frozenset({3}), heavy=False).outcome == "started"
    # 不能排队的任务遇到排队中的冲突也拒绝
    assert jobs.submit("rename 2", lambda t: "", accounts=frozenset({2}), heavy=False, queue=False).outcome == "busy"
    for label in ("delete 3", "sync 1"):
        gate.release(label)
    wait_until(lambda: "sync 2" in gate.started)
    assert "delete 2" not in gate.started
    gate.release("sync 2")
    gate.release("delete 2")
    assert jobs.wait(10)
    assert gate.started.index("delete 2") > gate.started.index("sync 2")


def test_same_key_is_not_added_twice(jobs):
    gate = Gate()
    first = jobs.submit("cycle", gate.task("cycle"), key="cycle")
    again = jobs.submit("cycle", gate.task("again"), key="cycle")
    assert again.outcome == "duplicate" and again.task is first.task
    gate.release("cycle")
    assert jobs.wait(10)
    # 结束后可以再次提交
    assert jobs.submit("cycle", lambda t: "", key="cycle").outcome == "started"


def test_cancel_queued_task_unblocks_the_next(jobs):
    gate = Gate()
    jobs.submit("sync 1", gate.task("sync 1"), accounts=frozenset({1}))
    blocker = jobs.submit("sync 2", gate.task("sync 2"), accounts=frozenset({2})).task
    jobs.submit("delete 2", gate.task("delete 2"), accounts=frozenset({2}), heavy=False)
    running = jobs.snapshot()[0][0]
    assert jobs.cancel(running.id) is None
    cancelled = jobs.cancel(blocker.id)
    assert cancelled.state == "cancelled" and cancelled.finished.is_set()
    # 挡在前面的任务取消后，删除马上开始
    wait_until(lambda: "delete 2" in gate.started)
    for label in ("sync 1", "delete 2"):
        gate.release(label)
    assert jobs.wait(10)
    assert "sync 2" not in gate.started
    assert jobs.finished_count == 3


def test_failed_task_is_recorded_and_queue_continues(jobs):
    jobs.submit("bad", lambda t: 1 / 0)
    jobs.submit("good", lambda t: "fine")
    assert jobs.wait(10)
    good, bad = jobs.history
    assert (bad.state, bad.ok) == ("failed", False) and "division by zero" in bad.summary
    assert (good.state, good.summary) == ("done", "fine")
