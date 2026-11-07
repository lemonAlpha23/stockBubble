"""Architecture guards that run as tests.

``.importlinter`` enforces the same rules for CI; these tests keep the guarantee
inside pytest so a local ``uv run pytest`` fails just as loudly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "stockbubble"

FORBIDDEN: dict[str, frozenset[str]] = {
    "domain": frozenset(
        {
            "fastapi",
            "starlette",
            "uvicorn",
            "httpx",
            "yaml",
            "stockbubble.adapters",
            "stockbubble.application",
            "stockbubble.api",
            "stockbubble.bootstrap",
            "stockbubble.cli",
        }
    ),
    "adapters": frozenset({"stockbubble.api", "stockbubble.application", "stockbubble.bootstrap"}),
    "application": frozenset({"fastapi", "starlette", "uvicorn", "stockbubble.api"}),
}


def module_files() -> list[Path]:
    return sorted(path for path in PACKAGE.rglob("*.py") if "__pycache__" not in path.parts)


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def layer_of(path: Path) -> str | None:
    relative = path.relative_to(PACKAGE)
    return relative.parts[0] if len(relative.parts) > 1 else None


@pytest.mark.parametrize("path", module_files(), ids=lambda path: path.name)
def test_layer_imports_are_allowed(path: Path) -> None:
    layer = layer_of(path)
    if layer is None or layer not in FORBIDDEN:
        return
    banned = FORBIDDEN[layer]
    violations = sorted(
        name
        for name in imported_modules(path)
        if any(name == entry or name.startswith(f"{entry}.") for entry in banned)
    )
    assert not violations, f"{path.relative_to(PACKAGE.parent.parent)} imports {violations}"


def test_domain_layer_has_no_io_modules() -> None:
    offenders = [
        path.name
        for path in sorted((PACKAGE / "domain").glob("*.py"))
        if any(
            name.split(".")[0] in {"asyncio", "socket", "http", "urllib", "subprocess"}
            for name in imported_modules(path)
        )
    ]
    assert not offenders, f"domain modules perform IO: {offenders}"


def test_package_layout_is_stable() -> None:
    assert {path.name for path in PACKAGE.iterdir() if path.is_dir()} >= {
        "adapters",
        "api",
        "application",
        "domain",
    }


async def test_every_provider_satisfies_the_ports() -> None:
    from stockbubble.adapters.coingecko import CoinGeckoProvider
    from stockbubble.adapters.yahoo import YahooProvider
    from stockbubble.domain.ports import MarketDataProvider, PriceSeriesProvider

    for provider in (CoinGeckoProvider(), YahooProvider()):
        assert isinstance(provider, MarketDataProvider)
        assert isinstance(provider, PriceSeriesProvider)
        await provider.aclose()
