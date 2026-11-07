"""Company logos for US equities.

Two key-free PNG endpoints, union coverage verified against the 258-symbol
universe (2026-11): parqet covers 256, companiesmarketcap covers 256, and the
union is 258/258. The four stragglers sit on exactly one side each
(``FOXA``/``GOOGL`` only on parqet, ``KIM``/``MRSH`` only on
companiesmarketcap), which is why the resolver hands out an ordered pair
instead of a single URL: the frontend tries the primary, swaps to the fallback
on a broken image, and only then falls back to the text layout.

No network access happens here — the URLs are deterministic, so quote building
stays offline and fast. Probing would cost 258 requests per cold start for
information the browser discovers for free while loading the image.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import NamedTuple


class LogoSources(NamedTuple):
    parqet: str = "https://assets.parqet.com/logos/symbol/{symbol}?format=png"
    companiesmarketcap: str = (
        "https://companiesmarketcap.com/img/company-logos/64/{symbol}.png"
    )


DEFAULT_ORDER: tuple[str, ...] = ("parqet", "companiesmarketcap")

#: Symbols whose first source is a dead end (404) — start with the other one.
FIRST_SOURCE: Mapping[str, str] = {
    "KIM": "companiesmarketcap",
    "MRSH": "companiesmarketcap",
}

#: Sources reject an empty path segment, so anything odd degrades to "no logo".
_SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")


class StockLogos:
    """Resolve a ticker to ``(primary, fallback)`` logo URLs."""

    def __init__(
        self,
        *,
        order: Sequence[str] = DEFAULT_ORDER,
        first_source: Mapping[str, str] = FIRST_SOURCE,
        sources: LogoSources | None = None,
    ) -> None:
        self._order = tuple(order)
        self._first = dict(first_source)
        self._sources = sources or LogoSources()
        missing = [name for name in self._order if name not in self._sources._fields]
        if missing:  # pragma: no cover - misconfiguration, fail loudly
            raise ValueError(f"unknown logo source(s): {', '.join(missing)}")

    def urls(self, symbol: str) -> tuple[str, str | None]:
        """Primary and (optional) fallback URL for ``symbol``."""
        ticker = symbol.strip().upper()
        if not ticker or not set(ticker) <= _SAFE:
            return "", None
        order = (self._first.get(ticker), *self._order)
        seen: list[str] = []
        for name in order:
            if name is None or name in seen:
                continue
            seen.append(name)
        template = getattr(self._sources, seen[0])
        primary = template.format(symbol=ticker)
        fallback = getattr(self._sources, seen[1]).format(symbol=ticker) if len(seen) > 1 else None
        return primary, fallback
