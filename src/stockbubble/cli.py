"""Command line entry point: ``serve`` / ``snapshot`` / ``check``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from stockbubble import __version__
from stockbubble.adapters.universes import YamlUniverseRepository
from stockbubble.api.app import create_app
from stockbubble.bootstrap import (
    build_providers,
    build_services,
    format_table,
    ui_settings,
    validate_universes,
)
from stockbubble.config import Settings
from stockbubble.domain.models import ChangeWindow, SizeBasis

LOGGER = logging.getLogger("stockbubble.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stockbubble",
        description="Bubble chart market dashboard (crypto + US equities).",
    )
    parser.add_argument("--version", action="version", version=f"stockbubble {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the API + web UI")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true", help="auto reload (development)")
    serve.add_argument("--log-level", default=None)

    snap = sub.add_parser("snapshot", help="print one snapshot as plain text")
    snap.add_argument("universe", nargs="?", default=None, help="universe id (default: first)")
    snap.add_argument("--window", default=None, help="1h | 24h | 7d | 30d")
    snap.add_argument("--size", default=SizeBasis.MARKET_CAP.value, help="market_cap | volume")
    snap.add_argument("--limit", type=int, default=None)

    sub.add_parser("check", help="validate settings + universe wiring offline")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
    logging.basicConfig(
        level=(getattr(args, "log_level", None) or settings.log_level).upper(),
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    if args.command == "check":
        return _check(settings)
    if args.command == "snapshot":
        return asyncio.run(_snapshot(settings, args))
    return _serve(settings, args)


def _serve(settings: Settings, args: argparse.Namespace) -> int:
    import uvicorn

    host = args.host or settings.host
    port = args.port or settings.port
    level = (args.log_level or settings.log_level).lower()
    print(f"→ http://{host}:{port}  (docs: /api/docs)")

    if args.reload:
        uvicorn.run(
            "stockbubble.api.asgi:app",
            host=host,
            port=port,
            log_level=level,
            reload=True,
            reload_dirs=[str(Path(__file__).resolve().parent)],
        )
        return 0

    services = build_services(settings)
    app = create_app(services=services, ui=ui_settings(settings), web_dir=settings.web_dir)
    uvicorn.run(app, host=host, port=port, log_level=level)
    return 0


def _check(settings: Settings) -> int:
    repository = YamlUniverseRepository.from_directory(settings.universes_dir)
    registry = build_providers(settings)
    validate_universes(repository.all(), registry)
    rows = ["universe  provider   class      windows            size"]
    for universe in repository.all():
        windows = ",".join(window.value for window in universe.windows)
        size = "dynamic" if universe.is_dynamic else f"{len(universe.instruments)} symbols"
        rows.append(
            f"{universe.id:<9} {universe.provider:<10} "
            f"{universe.asset_class.value:<10} {windows:<18} {size}"
        )
    print("\n".join(rows))
    print(f"ok: {len(repository.all())} universes, {len(registry.all())} providers")
    return 0


async def _snapshot(settings: Settings, args: argparse.Namespace) -> int:
    services = build_services(settings)
    try:
        universe_id = args.universe or services.universes.all()[0].id
        window = ChangeWindow.parse(args.window) if args.window else None
        try:
            basis = SizeBasis(args.size)
        except ValueError:
            print(f"unknown size basis {args.size!r}", file=sys.stderr)
            return 2
        snapshot = await services.bubble.snapshot(
            universe_id, window=window, size_basis=basis, limit=args.limit
        )
        print(
            f"{snapshot.universe_name} · window={snapshot.window.value} "
            f"basis={snapshot.size_basis.value} · {snapshot.generated_at:%Y-%m-%d %H:%M:%S}"
        )
        print(format_table(snapshot.nodes, snapshot.warnings))
        stats = snapshot.stats
        print(
            f"stats: provider={stats.provider_quotes} returned={stats.returned} "
            f"skipped={stats.skipped} trimmed={stats.trimmed} "
            f"coverage={stats.coverage:.0%}"
        )
        return 0
    finally:
        await services.aclose()


if __name__ == "__main__":
    raise SystemExit(main())
