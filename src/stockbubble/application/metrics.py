"""Pure sizing/ranking logic for the bubble canvas.

No I/O here on purpose: everything is a deterministic function of quotes plus
the requested window and size basis, which makes the board rules testable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from stockbubble.domain.models import (
    BubbleNode,
    ChangeWindow,
    DataQuality,
    Quote,
    SizeBasis,
)


def size_of(quote: Quote, basis: SizeBasis) -> float | None:
    """Bubble area driver for one quote, or ``None`` when unavailable."""
    candidate = quote.market_cap if basis is SizeBasis.MARKET_CAP else quote.volume
    if candidate is None or candidate <= 0:
        return None
    return candidate


@dataclass(frozen=True, slots=True)
class NodeBuild:
    nodes: tuple[BubbleNode, ...]
    skipped: int
    trimmed: int
    total_size: float


def build_nodes(
    quotes: Sequence[Quote],
    *,
    window: ChangeWindow,
    basis: SizeBasis,
    limit: int | None = None,
) -> NodeBuild:
    """Rank quotes by size, attach shares, and drop unusable rows.

    ``skipped`` counts quotes without a usable size value; ``trimmed`` counts
    quotes dropped purely because of ``limit``.
    """
    candidates: list[tuple[float, Quote]] = []
    skipped = 0
    for quote in quotes:
        size = size_of(quote, basis)
        if size is None:
            skipped += 1
            continue
        candidates.append((size, quote))

    candidates.sort(key=lambda item: (-item[0], item[1].symbol))
    trimmed = 0
    if limit is not None and len(candidates) > limit:
        trimmed = len(candidates) - limit
        candidates = candidates[:limit]

    total = sum(size for size, _ in candidates)
    nodes: list[BubbleNode] = []
    for rank, (size, quote) in enumerate(candidates, start=1):
        change = quote.change(window)
        quality = quote.quality
        if quality is DataQuality.OK and change is None:
            quality = DataQuality.PARTIAL
        nodes.append(
            BubbleNode(
                symbol=quote.symbol,
                name=quote.name,
                asset_class=quote.asset_class,
                sector=quote.sector,
                price=quote.price,
                currency=quote.currency,
                change_pct=change,
                changes=dict(quote.changes),
                size_value=size,
                share=(size / total) if total > 0 else 0.0,
                rank=rank,
                quality=quality,
                source=quote.source,
                logo_url=quote.logo_url,
        logo_fallback=quote.logo_fallback,
            )
        )
    return NodeBuild(
        nodes=tuple(nodes),
        skipped=skipped,
        trimmed=trimmed,
        total_size=total,
    )
