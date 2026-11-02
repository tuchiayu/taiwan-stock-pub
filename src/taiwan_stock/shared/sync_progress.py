"""Optional worker context; direct adapter/service calls remain usable in tests."""

from contextvars import ContextVar

current = ContextVar("sync_job_progress", default=None)


class Cancelled(BaseException):
    """Leave existing unit transactions through their interruption handlers."""


def checkpoint():
    if progress := current.get():
        progress.update()


def phase(label):
    if progress := current.get():
        progress.phase = label
        progress.update()
