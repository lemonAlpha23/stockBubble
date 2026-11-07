# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright>=1.44"]
# ///
# ruff: noqa: S101
"""Browser regression checks with deterministic market data, no upstream access.

Run: uv run --script scripts/check_frontend.py
Install a browser first: uv run --with playwright playwright install chromium
"""

from __future__ import annotations

import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_: object) -> None:
        pass


def main() -> None:
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT / "web"))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    windows = [{"value": v, "label": v} for v in ("1h", "24h", "7d", "30d")]
    universe = {
        "id": "test",
        "name": "Test",
        "asset_class": "crypto",
        "windows": ["24h", "7d"],
        "default_window": "24h",
        "default_size_basis": "volume",
        "currency": "USD",
    }
    nodes = [
        {
            "symbol": symbol,
            "name": symbol,
            "asset_class": "crypto",
            "sector": "Test",
            "price": 100,
            "currency": "USD",
            "change_pct": 1,
            "changes": {"24h": 1},
            "size_value": 100,
            "share": 0.01,
            "rank": i + 1,
            "quality": "ok",
            "source": "test",
        }
        for i, symbol in enumerate(["AAA", "BBB"] + [f"C{i}" for i in range(98)])
    ]
    config = {
        "title": "Test",
        "version": "0.1.0",
        "revision": "123456789abcdef",
        "windows": windows,
        "size_bases": [
            {"value": "market_cap", "label": "Cap"},
            {"value": "volume", "label": "Volume"},
        ],
        "auto_refresh_seconds": 3600,
        "color_clamp_pct": 10,
        "max_nodes": 400,
    }
    snapshot = {
        "universe": universe,
        "generated_at": "2026-11-07T00:00:00Z",
        "window": "24h",
        "size_basis": "volume",
        "currency": "USD",
        "total_size": 10000,
        "nodes": nodes,
        "warnings": [],
        "stats": {"returned": 100, "provider_quotes": 100, "coverage": 1},
    }
    try:
        with sync_playwright() as p:
            browser_type = getattr(p, os.getenv("BUBBLE_TEST_BROWSER", "chromium"))
            executable = os.getenv("BUBBLE_TEST_BROWSER_PATH")
            browser = browser_type.launch(**({"executable_path": executable} if executable else {}))
            page = browser.new_page(viewport={"width": 390, "height": 844}, has_touch=True)
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("""(() => {
              const raf = window.requestAnimationFrame;
              window.simFrames = 0;
              window.requestAnimationFrame = cb => raf.call(window, t => {
                if (cb.name === 'runSim') window.simFrames++;
                cb(t);
              });
              const now = Date.now;
              window.clockOffset = 0;
              Date.now = () => now() + window.clockOffset;
              const fetch = window.fetch.bind(window);
              window.historyCalls = [];
              window.historyAborts = 0;
              window.slowHistory = false;
              window.fetch = (url, options = {}) => {
                if (!String(url).startsWith('/api/history?')) return fetch(url, options);
                const params = new URL(url, location.href).searchParams;
                const symbol = params.get('symbol');
                const delayed = window.slowHistory && symbol === 'AAA';
                window.historyCalls.push(symbol);
                options.signal?.addEventListener('abort', () => window.historyAborts++);
                // Deliberately let an old failure arrive after cancellation, to test the UI guard.
                return new Promise((resolve, reject) => setTimeout(() => {
                  if (delayed) return reject(new Error('obsolete request failed'));
                  resolve(new Response(JSON.stringify({source: symbol, points: [
                    {t: 1762473600000, c: 100}, {t: 1762560000000, c: 110}
                  ]}), {status: 200}));
                }, delayed ? 500 : 10));
              };
            })();""")

            def api(route):
                path = route.request.url.split("?", 1)[0]
                payload = (
                    config
                    if path.endswith("/config")
                    else ([universe] if path.endswith("/universes") else snapshot)
                )
                route.fulfill(content_type="application/json", body=json.dumps(payload))

            page.route("**/api/**", api)
            page.goto(base)
            page.wait_for_selector("g.bubble")
            assert "1234567" in page.locator("#status-version").inner_text()
            assert not page.locator("#view-controls").is_visible()
            page.wait_for_timeout(4500)
            before = page.evaluate("window.simFrames")
            page.wait_for_timeout(600)
            assert page.evaluate("window.simFrames") == before, "Mobile simulation never rests"
            viewport = page.locator(".viewport").get_attribute("transform")
            chart = page.locator("#chart").bounding_box()
            page.mouse.move(chart["x"] + 3, chart["y"] + 3)
            page.mouse.down()
            page.mouse.move(chart["x"] + 80, chart["y"] + 40, steps=5)
            page.mouse.up()
            assert page.locator(".viewport").get_attribute("transform") == viewport
            bubble = page.locator("g.bubble").first.bounding_box()
            x, y = bubble["x"] + bubble["width"] / 2, bubble["y"] + bubble["height"] / 2
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x + 35, y + 30, steps=8)
            page.mouse.up()
            page.wait_for_timeout(100)
            assert page.evaluate("window.simFrames") > before, "Dragging did not wake simulation"

            def select(symbol: str) -> None:
                page.locator("#search").fill(symbol)
                page.locator("#search").press("Enter")

            select("AAA")
            page.wait_for_function(
                "document.getElementById('spark-meta').textContent.includes('AAA')"
            )
            select("BBB")
            page.wait_for_function(
                "document.getElementById('spark-meta').textContent.includes('BBB')"
            )
            count = page.evaluate("window.historyCalls.length")
            select("AAA")
            assert page.evaluate("window.historyCalls.length") == count, "Fresh cache missed"
            page.evaluate("window.clockOffset += 61000; window.slowHistory = true")
            select("AAA")
            assert "AAA" in page.locator("#spark-meta").inner_text(), (
                "Stale chart not shown immediately"
            )
            assert page.locator(".spark-line").get_attribute("d"), "Stale chart was cleared"
            pending_count = page.evaluate("window.historyCalls.length")
            select("AAA")
            assert page.evaluate("window.historyCalls.length") == pending_count, "Duplicate refresh"
            page.wait_for_timeout(650)
            assert "AAA" in page.locator("#spark-meta").inner_text(), "Failed refresh cleared cache"
            assert page.locator(".spark-line").get_attribute("d"), "Failed refresh cleared chart"
            select("AAA")
            select("BBB")
            page.wait_for_timeout(650)
            assert "BBB" in page.locator("#spark-meta").inner_text(), "Old failure overwrote chart"
            assert page.evaluate("window.historyAborts") > 0, "Old request not cancelled"
            assert page.evaluate("window.historyCalls.length") > count, (
                "Expired cache not refreshed"
            )
            page.locator("#detail-close").click()
            page.evaluate("document.getElementById('controls-toggle').click()")
            page.wait_for_timeout(80)
            middle = page.locator("#view-controls").bounding_box()["height"]
            page.wait_for_timeout(300)
            assert 0 < middle < page.locator("#view-controls").bounding_box()["height"]
            page.set_viewport_size({"width": 1440, "height": 900})
            assert page.locator("#view-controls").is_visible()
            assert not errors, errors
            browser.close()
            print(
                "PASS: mobile idle/wake, fixed canvas, history TTL/race/cancel, "
                "version, responsive controls"
            )
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
