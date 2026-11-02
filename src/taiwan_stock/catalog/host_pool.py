"""Per-host request queues for the ETF data sync (design 053).

One worker thread per host runs that host's requests strictly in order,
pausing for each job's delay afterwards, so no host ever sees more
requests than the old one-by-one loops sent. Different hosts run at the
same time. Workers only fetch: each job's outcome comes back through a
`Future`, and the caller writes to the database on its own thread in the
order it always did.
"""

import contextvars
import queue
import threading
from concurrent.futures import Future
from contextlib import contextmanager

from taiwan_stock.catalog.sync_history import SourceUnavailable
from taiwan_stock.shared import pacing, sync_progress

current = contextvars.ContextVar("host_pool", default=None)


class Skipped(Exception):
    """The host's circuit breaker opened before this job ran. `cause` is the
    maintenance page that closed the whole host (design 055), else `None`."""

    def __init__(self, cause=None):
        super().__init__(cause)
        self.cause = cause


class HostPool:
    def __init__(self):
        self._queues = {}
        self._threads = []
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._submitted = self._done = 0
        self._closed = False

    def submit(self, host, fetch, *, delay, pause=None, breaker=None):
        """Queue `fetch()` behind `host`'s earlier jobs.

        `breaker` is `(group, threshold, counts)`: within one `(host, group)`,
        `counts(exc)` failures in a row reaching `threshold` skips that
        group's remaining jobs without a request, as the loops' own
        breakers did.
        """
        future = Future()
        future.add_done_callback(self._finished)
        # Design 073: queued under the lock, so close() never puts its stop
        # marker ahead of a job that is still being submitted.
        with self._lock:
            if self._closed:
                raise RuntimeError("HostPool 已關閉")
            self._submitted += 1
            jobs = self._queues.get(host)
            if jobs is None:
                jobs = self._queues[host] = queue.Queue()
                worker = threading.Thread(
                    target=contextvars.copy_context().run,
                    args=(self._work, jobs),
                    name=f"host-pool:{host}",
                    daemon=True,
                )
                self._threads.append(worker)
                worker.start()
            jobs.put((future, fetch, delay, pause or pacing.pause, breaker))
        return future

    def _finished(self, _future):
        with self._lock:
            self._done += 1

    def counts(self):
        """`(finished, queued)` requests so far (design 054 §4.2.1)."""
        with self._lock:
            return self._done, self._submitted

    def _work(self, jobs):
        # Progress and cancellation live on the caller's database connection,
        # which SQLite keeps to its own thread: the caller checks them per
        # unit and stops the pool (`close(stop=True)`) when cancelled.
        sync_progress.current.set(None)
        failures = {}
        maintenance = None  # design 055: one maintenance page closes the host
        while True:
            item = jobs.get()
            if item is None:
                return
            future, fetch, delay, pause, breaker = item
            group = breaker[0] if breaker else None
            if maintenance is not None:
                future.set_exception(Skipped(maintenance))
                continue
            if self._stopping.is_set() or (
                breaker and failures.get(group, 0) >= breaker[1]
            ):
                future.set_exception(Skipped())
                continue
            try:
                future.set_result(fetch())
                failures[group] = 0
            except BaseException as exc:  # noqa: BLE001 - handed to the caller
                future.set_exception(exc)
                if isinstance(exc, SourceUnavailable):
                    maintenance = exc
                if breaker:
                    failures[group] = (
                        failures.get(group, 0) + 1 if breaker[2](exc) else 0
                    )
            finally:
                if not self._stopping.is_set() and delay:
                    pause(delay)

    def close(self, *, stop=False):
        """Wait for the workers; when stopping, skip queued jobs and leave a
        request still in flight to its daemon thread (design 054 §4.2.2)."""
        if stop:
            self._stopping.set()
        with self._lock:
            self._closed = True
            for jobs in self._queues.values():
                jobs.put(None)
            threads, self._threads = self._threads, []
        if not stop:
            for worker in threads:
                worker.join()


@contextmanager
def shared():
    """The pool already open for this job, or a new one for a lone sync."""
    pool = current.get()
    if pool is not None:
        yield pool
        return
    pool = HostPool()
    token = current.set(pool)
    stop = True
    try:
        yield pool
        stop = False
    finally:
        current.reset(token)
        pool.close(stop=stop)
        if progress := sync_progress.current.get():
            progress.detail = None


WAIT_SECONDS = 1


def wait(future):
    """Wait for a queued request without going quiet: every second the
    caller's progress shows how many requests are done and a cancel is
    noticed (design 054 §4.2.2). Returns the finished `future`."""
    while True:
        _report()
        try:
            future.exception(timeout=WAIT_SECONDS)
            return future
        except TimeoutError:
            sync_progress.checkpoint()


def _report():
    progress, pool = sync_progress.current.get(), current.get()
    if progress is not None and pool is not None:
        done, queued = pool.counts()
        progress.detail = f"已取得 {done}/{queued}"


def start(steps):
    """Run a sync generator up to its `yield`, i.e. until it has queued
    its requests, so several syncs' requests overlap. Errors before that
    point (for example a busy source lock) are raised here; the returned
    callable finishes the sync and returns its result."""
    try:
        next(steps)
    except StopIteration as done:
        value = done.value
        return lambda: value

    def finish():
        try:
            next(steps)
        except StopIteration as done:
            return done.value
        steps.close()
        raise RuntimeError("同步步驟未正常結束")

    return finish


def run(steps):
    """A sync generator on its own, start to finish."""
    with shared():
        return start(steps)()
