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
                if (window.historyRateLimited) return Promise.resolve(new Response(JSON.stringify({
                  error: {code: 'provider_rate_limited', message: 'Rate limit'}
                }), {status: 429, headers: {'Retry-After': '60'}}));
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
            # Inspect anchors as well as pixels: spring resets can take several frames.
            regression = browser.new_page(
                viewport={"width": 1440, "height": 900}, reduced_motion="no-preference"
            )
            regression.route("**/api/**", api)
            app_source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
            regression.route("**/app.js*", lambda route: route.fulfill(
                content_type="text/javascript",
                body=app_source.replace("  void boot();", """
                  window.inspectBubbles = () => sim.nodes.map(n => ({
                    symbol: n.symbol, x: n.x, y: n.y, homeX: n.homeX, homeY: n.homeY
                  }));
                  window.triggerAutoRefresh = () => { state.remaining = 0; tick(); };
                  window.checkShuffleDirections = () => {
                    const seed = state.layoutSeed;
                    let p = {x: sim.width / 2, y: sim.height / 2};
                    const entry = {symbol: 'direction-check', r: 20,
                      origin: {...p}, body: {getPosition: () => p}};
                    const results = [];
                    for (let i = 0; i < 20; i++) {
                      state.layoutSeed = i + 1;
                      const previous = entry.shuffleDirection;
                      const next = shuffleTarget(entry);
                      const direction = entry.shuffleDirection;
                      results.push({
                        alignment: previous ? Math.abs(previous.x * direction.x +
                          previous.y * direction.y) : 0,
                        originDistance: Math.hypot(next.x - entry.origin.x,
                          next.y - entry.origin.y),
                        distance: next.travel
                      });
                      p = {x: next.x, y: next.y};
                    }
                    state.layoutSeed = seed;
                    return results;
                  };
                  sim.float = false;
                  const originalSyncSim = syncSim;
                  syncSim = (leaves, relayout, animate) => {
                    const previous = new Map(sim.nodes.map(n => [n.symbol, {
                      body: n.body, x: n.body.getPosition().x, y: n.body.getPosition().y
                    }]));
                    originalSyncSim(leaves, relayout, animate);
                    if (animate) window.refreshTransition = sim.nodes.map(n => {
                      const old = previous.get(n.symbol);
                      const p = n.body.getPosition();
                      return {
                        retained: old && old.body === n.body,
                        jump: old ? Math.hypot(p.x - old.x, p.y - old.y) : 0,
                        distance: Math.hypot(p.x - n.homeX, p.y - n.homeY)
                      };
                    });
                  };
                  void boot();
                """),
            ))
            regression.goto(base)
            regression.wait_for_selector("g.bubble")
            regression.wait_for_timeout(2000)
            directions = regression.evaluate("checkShuffleDirections()")
            assert all(n["alignment"] <= 0.71 for n in directions), (
                "Shuffle repeated or reversed the previous direction"
            )
            assert all(n["originDistance"] >= 23.99 for n in directions), (
                "Shuffle returned to its original position"
            )
            assert all(n["distance"] <= 48.01 for n in directions)
            print("PASS: twenty local shuffles vary direction and avoid the origin")
            initial = regression.evaluate("inspectBubbles()")
            target = regression.locator("g.bubble").first
            box = target.bounding_box()
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            regression.mouse.move(x, y)
            regression.mouse.down()
            for step in range(1, 21):
                regression.mouse.move(x + 100 * step / 20, y + 70 * step / 20)
                regression.wait_for_timeout(20)
            regression.mouse.up()
            regression.wait_for_timeout(1500)
            anchors = regression.evaluate("inspectBubbles()")
            assert any(
                abs(old["homeX"] - new["homeX"]) > 10
                or abs(old["homeY"] - new["homeY"]) > 10
                for old, new in zip(initial, anchors, strict=True)
            ), "Drag did not change an anchor"
            # Opening and closing details must preserve dragged anchors.
            target.click(force=True)
            regression.wait_for_timeout(1000)

            def assert_anchors_preserved() -> None:
                current = {n["symbol"]: n for n in regression.evaluate("inspectBubbles()")}
                # Choose anchors inside both drawer widths, so boundary clamping is irrelevant.
                stable = [n for n in anchors if n["homeX"] < 700]
                assert stable
                for old in stable:
                    new = current[old["symbol"]]
                    assert abs(new["homeX"] - old["homeX"]) < 2, "Horizontal anchor reset"
                    assert abs(new["homeY"] - old["homeY"]) < 2, "Vertical anchor reset"

            assert_anchors_preserved()
            regression.locator("#detail-close").click()
            regression.wait_for_timeout(700)
            assert_anchors_preserved()
            print("PASS: dragged anchors survive drawer open/close")
            for trigger in ("manual", "manual", "automatic", "automatic"):
                before_shuffle = regression.evaluate("inspectBubbles()")
                with regression.expect_response("**/api/bubbles?*"):
                    if trigger == "manual":
                        regression.locator("#refresh").click()
                    else:
                        regression.evaluate("triggerAutoRefresh()")
                regression.wait_for_timeout(500)
                after_shuffle = regression.evaluate("inspectBubbles()")
                transition = regression.evaluate("window.refreshTransition")
                assert transition and all(n["retained"] for n in transition), (
                    "Refresh replaced physical bodies"
                )
                assert max(n["jump"] for n in transition) < 0.01, "Refresh teleported bubbles"
                assert max(n["distance"] for n in transition) > 10, "Missing movement targets"
                assert max(n["distance"] for n in transition) <= 48.01, (
                    "Refresh target moved outside the local shuffle range"
                )
                # Small, slow shuffles need not cross the impact-flash speed threshold.
                assert any(
                    abs(old["homeX"] - new["homeX"]) > 10
                    or abs(old["homeY"] - new["homeY"]) > 10
                    for old, new in zip(before_shuffle, after_shuffle, strict=True)
                ), f"{trigger} refresh did not randomize positions"
            print("PASS: manual/automatic refresh animates random layouts with collisions")
            regression.wait_for_timeout(8_500)
            slowing = regression.evaluate("inspectBubbles()")
            regression.wait_for_timeout(200)
            slowed = regression.evaluate("inspectBubbles()")
            assert max(
                ((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5
                for a, b in zip(slowing, slowed, strict=True)
            ) < 0.5, "Bubbles did not slow smoothly before stopping"
            regression.wait_for_timeout(1_400)
            settled = regression.evaluate("inspectBubbles()")
            regression.wait_for_timeout(600)
            assert settled == regression.evaluate("inspectBubbles()"), (
                "Refresh kept moving bubbles after ten seconds"
            )
            print("PASS: refreshed board remains still after ten seconds")
            regression.close()
            page.bring_to_front()
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
            assert page.locator(".spark-axis text").count() >= 2, "Missing price ticks"
            page.locator("#sparkline").scroll_into_view_if_needed()
            chart = page.locator("#sparkline").bounding_box()
            assert chart
            page.mouse.move(chart["x"] + chart["width"] * 0.3, chart["y"] + chart["height"] / 2)
            assert page.locator(".spark-hover").get_attribute("display") is None
            assert page.locator(".spark-hover text").first.text_content(), "Missing hover price"
            previous_x = float(page.locator(".spark-hover circle").get_attribute("cx"))
            previous_price = page.locator(".spark-hover text").first.text_content()
            page.mouse.move(chart["x"] + chart["width"] * 0.3 + 2, chart["y"] + chart["height"] / 2)
            current_x = float(page.locator(".spark-hover circle").get_attribute("cx"))
            assert 0 < current_x - previous_x < 4, "Hover still snaps to sampled points"
            assert page.locator(".spark-hover text").first.text_content() != previous_price
            assert "≈" in page.locator(".spark-hover text").first.text_content()
            page.mouse.move(0, 0)
            assert page.locator(".spark-hover").get_attribute("display") == "none"
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
            page.evaluate("window.historyRateLimited = true")
            page.locator('#timeframes button[data-value="1y"]').click()
            page.wait_for_function(
                "document.getElementById('spark-meta').textContent.includes('Rate limit')"
            )
            limited_calls = page.evaluate("window.historyCalls.length")
            page.locator('#timeframes button[data-value="3mo"]').click()
            assert page.evaluate("window.historyCalls.length") == limited_calls, "Cooldown ignored"
            page.evaluate("window.clockOffset += 61000; window.historyRateLimited = false")
            page.locator('#timeframes button[data-value="3mo"]').click()
            page.wait_for_function(
                "document.getElementById('spark-meta').textContent.includes('BBB')"
            )
            assert page.evaluate("window.historyCalls.length") > limited_calls, (
                "Cooldown never ended"
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
