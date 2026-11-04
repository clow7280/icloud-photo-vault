import threading
from contextlib import contextmanager

import pytest

from icloud_photo_vault import stats
from icloud_photo_vault.stats import StatsCache


@pytest.fixture
def calls(monkeypatch):
    """把统计查询换成计数器：每次返回第几次计算，可以用 gate 卡住计算。"""
    record = {"n": 0, "gate": None}

    def fake_month_stats(_conn, account_id, cutoff):
        if record["gate"] is not None:
            record["gate"].wait(5)
        record["n"] += 1
        return [{"account": account_id, "cutoff": cutoff, "n": record["n"]}]

    monkeypatch.setattr(stats, "month_stats", fake_month_stats)
    return record


@contextmanager
def no_connection():
    yield None


def wait_refresh(cache):
    for thread in threading.enumerate():
        if thread is not threading.current_thread() and thread.daemon:
            thread.join(5)


def test_cache_returns_stale_rows_and_refreshes_in_background(calls):
    cache = StatsCache(no_connection, max_age=60)
    assert cache.get(1, "c")[0]["n"] == 1
    assert cache.get(1, "c")[0]["n"] == 1
    assert calls["n"] == 1
    # 分界时间变了：先返回旧结果，后台重算后才换成新的
    assert cache.get(1, "d")[0]["n"] == 1
    wait_refresh(cache)
    assert cache.get(1, "d")[0] == {"account": 1, "cutoff": "d", "n": 2}


def test_cache_expires_after_max_age(calls):
    cache = StatsCache(no_connection, max_age=0)
    cache.get(1, "c")
    assert cache.get(1, "c")[0]["n"] == 1
    wait_refresh(cache)
    assert calls["n"] == 2


def test_invalidate_recomputes_and_drops_older_refresh(calls):
    cache = StatsCache(no_connection, max_age=0)
    cache.get(1, "c")
    calls["gate"] = threading.Event()
    cache.get(1, "c")  # 后台重算被卡住
    cache.invalidate()
    calls["gate"].set()
    wait_refresh(cache)
    # 失效之前开始的重算不会写回，失效后当场重新计算
    calls["gate"] = None
    before = calls["n"]
    assert cache.get(1, "c")[0]["n"] == before + 1
