"""Waits that pace requests to external sources (design 066).

Kept apart from other `time.sleep` calls so tests can skip only these.
"""

import time

from taiwan_stock.shared import sync_progress


def pause(seconds: float) -> None:
    # Design 081: a cancelled sync stops at the next wait between requests,
    # not only between units (one unit can page through a long index).
    sync_progress.checkpoint()
    time.sleep(seconds)
