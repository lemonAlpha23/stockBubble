"""Start the local dashboard and open a browser once its health check succeeds."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    host, port = "127.0.0.1", 8000
    # Leave existing services alone and choose an available local port.
    for port in range(8000, 8011):
        with socket.socket() as probe:
            try:
                probe.bind((host, port))
            except OSError:
                continue
            break
    else:
        print("Ports 8000-8010 are all in use. Close an existing server and try again.")
        return 1
    url = f"http://{host}:{port}"

    print(f"Starting Stock Bubble at {url}. Press Ctrl+C to stop.", flush=True)
    process = subprocess.Popen(  # noqa: S603 - fixed module and arguments, no shell
        [
            sys.executable,
            "-m",
            "uvicorn",
            "stockbubble.api.asgi:app",
            "--host",
            host,
            "--port",
            str(port),
        ],
        cwd=root,
    )
    try:
        deadline = time.monotonic() + 60
        while process.poll() is None:
            try:
                with urllib.request.urlopen(  # noqa: S310 - fixed localhost HTTP URL
                    f"{url}/api/health", timeout=1
                ) as response:
                    json.load(response)
                if not webbrowser.open(url):
                    print(f"Open this address in your browser: {url}", flush=True)
                return process.wait()
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                if time.monotonic() >= deadline:
                    print("The server did not become ready within 60 seconds.", flush=True)
                    return 1
                time.sleep(0.3)
        return process.returncode or 1
    except KeyboardInterrupt:
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
