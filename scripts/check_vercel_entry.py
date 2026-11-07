"""Smoke-test the Vercel function entry without deploying.

Usage:  uv run python scripts/check_vercel_entry.py
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def main() -> int:
    app = importlib.import_module("api.index").app
    transport = httpx.ASGITransport(app=app)
    failures = 0
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        checks = [
            ("/api/health", 200),
            ("/api/universes", 200),
            ("/api/bubbles?universe=crypto&window=24h&limit=5", 200),
            ("/", 200),
            ("/app.js", 200),
            ("/vendor/planck.min.js", 200),
        ]
        for path, expected in checks:
            response = await client.get(path)
            ok = response.status_code == expected
            failures += 0 if ok else 1
            print(f"{'OK  ' if ok else 'FAIL'} {path:52s} {response.status_code} ({len(response.content)}B)")
        payload = json.loads((await client.get("/api/bubbles?universe=us_equity&window=24h&limit=3")).text)
        print(f"us_equity: {len(payload['nodes'])} nodes, warnings={[w['code'] for w in payload['warnings']]}")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
