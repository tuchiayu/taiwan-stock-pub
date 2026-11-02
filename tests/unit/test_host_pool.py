"""Design 053 §4.2.1: per-host request queues."""

import threading
import time

import pytest

from taiwan_stock.catalog import host_pool
from taiwan_stock.shared import sync_progress


def _pool():
    return host_pool.HostPool()


def test_one_host_runs_its_jobs_one_at_a_time_in_order_with_its_delay():
    pool, running, peak, order, pauses = _pool(), [0], [0], [], []
    lock = threading.Lock()

    def job(n):
        def fetch():
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.01)
            order.append(n)
            with lock:
                running[0] -= 1
            return n

        return fetch

    futures = [
        pool.submit("twse", job(n), delay=2.0, pause=pauses.append) for n in range(4)
    ]
    assert [f.result() for f in futures] == [0, 1, 2, 3]
    pool.close()

    assert peak[0] == 1
    assert order == [0, 1, 2, 3]
    assert pauses == [2.0] * 4


def test_different_hosts_run_at_the_same_time():
    pool = _pool()
    both_waiting = threading.Barrier(2, timeout=2)

    futures = [
        pool.submit(host, both_waiting.wait, delay=0) for host in ("twse", "tpex")
    ]

    # Each job only returns once the other host's job is running too.
    assert all(f.exception() is None for f in futures)
    pool.close()


def test_an_error_comes_back_to_the_caller():
    pool = _pool()

    def fail():
        raise ValueError("來源格式不符")

    future = pool.submit("twse", fail, delay=0)

    with pytest.raises(ValueError, match="來源格式不符"):
        future.result()
    pool.close()


def test_breaker_skips_the_rest_of_one_group_without_requests():
    pool, calls = _pool(), []

    def blocked():
        calls.append("blocked")
        raise ConnectionError("403")

    def fine():
        calls.append("fine")
        return "ok"

    breaker = ("distributions", 2, lambda exc: isinstance(exc, ConnectionError))
    first = [pool.submit("tpex", blocked, delay=0, breaker=breaker) for _ in range(4)]
    other = pool.submit("tpex", fine, delay=0, breaker=("basic", 2, lambda e: True))

    skipped = [isinstance(f.exception(), host_pool.Skipped) for f in first]
    assert skipped == [False, False, True, True]
    assert other.result() == "ok"  # another group on the same host still runs
    assert calls == ["blocked", "blocked", "fine"]
    pool.close()


def test_an_uncounted_error_or_a_success_resets_the_breaker():
    pool, outcomes = _pool(), iter(["block", "other", "block", "ok", "block"])

    def fetch():
        kind = next(outcomes)
        if kind == "block":
            raise ConnectionError(kind)
        if kind == "other":
            raise ValueError(kind)
        return kind

    breaker = ("g", 2, lambda exc: isinstance(exc, ConnectionError))
    futures = [pool.submit("h", fetch, delay=0, breaker=breaker) for _ in range(5)]

    assert not any(isinstance(f.exception(), host_pool.Skipped) for f in futures)
    pool.close()


def test_stopping_skips_queued_jobs():
    pool, started = _pool(), threading.Event()
    release = threading.Event()

    def slow():
        started.set()
        release.wait(2)

    first = pool.submit("h", slow, delay=0)
    queued = pool.submit("h", lambda: "never", delay=0)
    started.wait(2)
    closer = threading.Thread(target=pool.close, kwargs={"stop": True})
    closer.start()
    release.set()
    closer.join(2)

    assert first.exception() is None
    assert isinstance(queued.exception(), host_pool.Skipped)


def test_workers_never_touch_the_callers_progress():
    """It lives on the caller's SQLite connection, bound to its thread."""

    class Progress:
        def update(self):
            raise AssertionError("worker used the caller's progress")

    token = sync_progress.current.set(Progress())
    try:
        pool = _pool()
        future = pool.submit("h", sync_progress.checkpoint, delay=0)
        assert future.exception() is None
        pool.close()
    finally:
        sync_progress.current.reset(token)


def test_start_raises_early_errors_and_finish_returns_the_result():
    def busy():
        raise RuntimeError("此來源同步中")
        yield  # pragma: no cover

    def steps():
        yield
        return "done"

    with pytest.raises(RuntimeError, match="此來源同步中"):
        host_pool.start(busy())
    assert host_pool.start(steps())() == "done"


def test_shared_reuses_the_open_pool():
    with host_pool.shared() as outer, host_pool.shared() as inner:
        assert inner is outer
    assert host_pool.current.get() is None


class _Progress:
    """Stands in for the job's `Progress`: records what `wait` reports."""

    def __init__(self, cancel_after=None):
        self.detail, self.details, self.updates = None, [], 0
        self.cancel_after = cancel_after

    def update(self):
        self.updates += 1
        self.details.append(self.detail)
        if self.cancel_after is not None and self.updates >= self.cancel_after:
            raise sync_progress.Cancelled()


def test_counts_cover_successes_failures_and_skips():
    pool = _pool()
    breaker = ("g", 1, lambda exc: True)

    def fail():
        raise ConnectionError("403")

    futures = [pool.submit("h", fail, delay=0, breaker=breaker) for _ in range(2)]
    futures.append(pool.submit("h2", lambda: 1, delay=0))
    for future in futures:
        future.exception()
    pool.close()

    assert pool.counts() == (3, 3)


def test_wait_reports_fetched_requests_every_second(monkeypatch):
    """Design 054 §4.2.2: the caller never goes quiet while it waits."""
    monkeypatch.setattr(host_pool, "WAIT_SECONDS", 0.01)
    progress, release = _Progress(), threading.Event()
    token = sync_progress.current.set(progress)
    try:
        with host_pool.shared() as pool:
            pool.submit("fast", lambda: "done", delay=0).result()
            slow = pool.submit("slow", lambda: release.wait(2), delay=0)
            threading.Timer(0.1, release.set).start()
            assert host_pool.wait(slow) is slow
        assert progress.updates >= 2
        assert progress.details[0] == "已取得 1/2"
        assert progress.detail is None  # cleared once the pool closes
    finally:
        sync_progress.current.reset(token)


def test_wait_stops_at_once_on_cancel_without_the_request(monkeypatch):
    monkeypatch.setattr(host_pool, "WAIT_SECONDS", 0.01)
    progress, release = _Progress(cancel_after=2), threading.Event()
    token = sync_progress.current.set(progress)
    started = time.monotonic()
    try:
        with pytest.raises(sync_progress.Cancelled):
            with host_pool.shared() as pool:
                host_pool.wait(pool.submit("slow", lambda: release.wait(5), delay=0))
    finally:
        sync_progress.current.reset(token)
        release.set()

    assert time.monotonic() - started < 1  # not the request's 5 seconds


def test_a_maintenance_page_closes_the_whole_host_with_its_cause():
    """Design 055: every later job of that host, in any group, is skipped
    without a request and carries the maintenance page; other hosts run."""
    from taiwan_stock.catalog.sync_history import SourceUnavailable

    pool, calls = _pool(), []

    def down():
        calls.append("down")
        raise SourceUnavailable("元大網站維護中")

    def fine():
        calls.append("fine")
        return "ok"

    first = pool.submit("元大", down, delay=0, breaker=("trust", 5, lambda e: True))
    later = [pool.submit("元大", fine, delay=0, breaker=("other", 5, lambda e: True))]
    later.append(pool.submit("元大", fine, delay=0))
    elsewhere = pool.submit("國泰", fine, delay=0)

    assert isinstance(first.exception(), SourceUnavailable)
    for future in later:
        skipped = future.exception()
        assert isinstance(skipped, host_pool.Skipped)
        assert isinstance(skipped.cause, SourceUnavailable)
    assert elsewhere.result() == "ok"
    assert calls.count("fine") == 1  # only 國泰
    pool.close()


def test_the_ordinary_breaker_skips_without_a_cause():
    pool = _pool()

    def blocked():
        raise ConnectionError("403")

    futures = [
        pool.submit("h", blocked, delay=0, breaker=("g", 1, lambda e: True))
        for _ in range(2)
    ]

    assert futures[1].exception().cause is None
    pool.close()
