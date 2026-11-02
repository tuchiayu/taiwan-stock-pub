"""Private, shell-free worker entry point: resolved data directory and job ID."""

import sys
from pathlib import Path

from taiwan_stock.infrastructure.sync_jobs import run_worker

if __name__ == "__main__":
    run_worker(Path(sys.argv[1]), sys.argv[2])
