"""Design 081: pacing waits are cancellation checkpoints."""

import importlib.util

import pytest

from taiwan_stock.shared import sync_progress

# conftest replaces `pacing.pause` for every test; load the real one.
_spec = importlib.util.find_spec("taiwan_stock.shared.pacing")
real_pacing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_pacing)


class CancelledProgress:
    def update(self):
        raise sync_progress.Cancelled()


def test_a_wait_in_a_cancelled_job_stops_before_sleeping(monkeypatch):
    slept = []
    monkeypatch.setattr(real_pacing.time, "sleep", slept.append)
    token = sync_progress.current.set(CancelledProgress())
    try:
        with pytest.raises(sync_progress.Cancelled):
            real_pacing.pause(3)
    finally:
        sync_progress.current.reset(token)
    assert slept == []


def test_a_wait_outside_a_job_just_sleeps(monkeypatch):
    slept = []
    monkeypatch.setattr(real_pacing.time, "sleep", slept.append)

    real_pacing.pause(3)

    assert slept == [3]
