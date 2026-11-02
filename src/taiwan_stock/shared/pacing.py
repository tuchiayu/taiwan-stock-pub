"""Waits that pace requests to external sources (design 066).

Kept apart from other `time.sleep` calls so tests can skip only these.
"""

import time


def pause(seconds: float) -> None:
    time.sleep(seconds)
