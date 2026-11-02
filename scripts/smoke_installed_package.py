"""Start the installed `taiwan-stock` and check that the Web answers (design 066).

Run with the Python of an environment that has only the wheel installed, so a
missing dependency or package file fails here instead of on a user's machine.
Standard library only.
"""

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

STARTUP_SECONDS = 60


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read()


def main():
    command = shutil.which("taiwan-stock", path=str(Path(sys.executable).parent))
    if command is None:
        sys.exit("找不到 taiwan-stock 執行檔")
    with tempfile.TemporaryDirectory() as temp:
        subprocess.run([command, "--version"], check=True, cwd=temp)
        port = free_port()
        web = subprocess.Popen(
            [
                command,
                "web",
                "--no-browser",
                "--port",
                str(port),
                "--data-dir",
                str(Path(temp) / "data"),
            ],
            cwd=temp,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + STARTUP_SECONDS
            health = None
            while time.monotonic() < deadline and web.poll() is None:
                try:
                    status, body = get(f"{base}/health")
                    health = json.loads(body)
                    break
                except OSError:
                    time.sleep(0.5)
            if health is None or health.get("status") != "ok":
                raise SystemExit(f"/health 未就緒：{health}")
            status, _ = get(f"{base}/")
            if status != 200:
                raise SystemExit(f"首頁回應 {status}")
            print(f"smoke ok: {health}")
        except BaseException:
            web.terminate()
            output, _ = web.communicate(timeout=10)
            print(output, file=sys.stderr)
            raise
        web.terminate()
        web.wait(timeout=10)


if __name__ == "__main__":
    main()
