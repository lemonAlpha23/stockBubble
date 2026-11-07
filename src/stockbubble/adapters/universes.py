"""YAML backed universe configuration.

Universe documents are *data*, not code: adding a new basket (HK equities, ETFs,
FX…) never requires touching the application layer. Each file may hold one
universe mapping or a ``universes:`` list.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from stockbubble.domain.errors import UniverseConfigError, UniverseNotFound
from stockbubble.domain.models import Universe

SUPPORTED_SUFFIXES = frozenset({".yaml", ".yml"})


class YamlUniverseRepository:
    """In-memory, validated view over a directory of universe files."""

    def __init__(self, universes: Sequence[Universe]) -> None:
        ordered: dict[str, Universe] = {}
        for universe in universes:
            if universe.id in ordered:
                msg = f"duplicate universe id {universe.id!r}"
                raise UniverseConfigError(msg)
            ordered[universe.id] = universe
        if not ordered:
            msg = "no universes configured"
            raise UniverseConfigError(msg)
        self._by_id = ordered

    @classmethod
    def from_directory(cls, directory: Path) -> YamlUniverseRepository:
        root = directory.expanduser()
        if not root.is_dir():
            msg = f"universes directory not found: {root}"
            raise UniverseConfigError(msg)
        files = sorted(
            path
            for path in root.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
        )
        if not files:
            msg = f"no *.yaml universe files in {root}"
            raise UniverseConfigError(msg)
        universes: list[Universe] = []
        for path in files:
            universes.extend(cls._load_file(path))
        return cls(universes)

    def get(self, universe_id: str) -> Universe:
        universe = self._by_id.get(universe_id.strip().lower())
        if universe is None:
            known = ", ".join(self._by_id) or "<none>"
            raise UniverseNotFound(f"unknown universe {universe_id!r}", hint=f"known: {known}")
        return universe

    def all(self) -> tuple[Universe, ...]:
        return tuple(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)

    @staticmethod
    def _load_file(path: Path) -> list[Universe]:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            msg = f"{path.name}: cannot read universe file ({exc})"
            raise UniverseConfigError(msg) from exc
        entries = _entries_of(raw, path)
        loaded: list[Universe] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                msg = f"{path.name}: universe entry #{index} must be a mapping"
                raise UniverseConfigError(msg)
            try:
                loaded.append(Universe.model_validate(entry))
            except ValidationError as exc:
                msg = f"{path.name}: invalid universe entry #{index}: {_short_errors(exc)}"
                raise UniverseConfigError(msg) from exc
        return loaded


def _entries_of(raw: Any, path: Path) -> list[Any]:
    if isinstance(raw, dict):
        nested = raw.get("universes")
        if nested is None:
            return [raw]
        if not isinstance(nested, list):
            msg = f"{path.name}: 'universes' must be a list"
            raise UniverseConfigError(msg)
        return list(nested)
    if isinstance(raw, list):
        return list(raw)
    msg = f"{path.name}: expected a mapping or a list of universes"
    raise UniverseConfigError(msg)


def _short_errors(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors()[:4]:
        location = ".".join(str(item) for item in error["loc"]) or "<root>"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)
