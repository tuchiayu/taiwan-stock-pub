"""Design 073: submitting and closing a HostPool never strand a job."""

import queue
import threading

import pytest

from taiwan_stock.catalog import host_pool


def test_a_close_during_a_submit_still_runs_the_job(monkeypatch):
    putting, proceed = threading.Event(), threading.Event()

    class PausingQueue(queue.Queue):
        def put(self, item, *args, **kwargs):
            if item is not None:
                putting.set()
                proceed.wait(5)
            super().put(item, *args, **kwargs)

    monkeypatch.setattr(host_pool.queue, "Queue", PausingQueue)
    pool = host_pool.HostPool()
    submitted = {}
    submitter = threading.Thread(
        target=lambda: submitted.setdefault(
            "future",
            pool.submit("twse", lambda: "done", delay=0, pause=lambda _: None),
        )
    )
    submitter.start()
    assert putting.wait(5)
    closer = threading.Thread(target=pool.close)
    closer.start()
    # Give close() the chance to run before the job reaches the queue.
    closer.join(0.2)
    proceed.set()
    submitter.join(5)
    closer.join(5)
    assert submitted["future"].result(timeout=2) == "done"


def test_a_submit_after_close_is_refused():
    pool = host_pool.HostPool()
    pool.submit("twse", lambda: 1, delay=0, pause=lambda _: None).result(timeout=2)
    pool.close()
    with pytest.raises(RuntimeError, match="HostPool 已關閉"):
        pool.submit("twse", lambda: 2, delay=0, pause=lambda _: None)
