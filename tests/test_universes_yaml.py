from __future__ import annotations

from pathlib import Path

import pytest

from stockbubble.adapters.universes import YamlUniverseRepository
from stockbubble.domain.errors import UniverseConfigError, UniverseNotFound
from stockbubble.domain.models import Universe


def test_bundled_markets_have_labels_and_can_be_serialized() -> None:
    from stockbubble.api.schemas import UniverseDTO
    from stockbubble.config import Settings

    repo = YamlUniverseRepository.from_directory(Settings().universes_dir)
    for key, currency, count in [("hk_equity", "HKD", 200), ("a_equity", "CNY", 200)]:
        universe = repo.get(key)
        assert universe.asset_class.label
        assert universe.currency == currency
        assert len(universe.instruments) == count
        assert len(set(universe.symbols())) == count
        assert universe.limit == count
        assert UniverseDTO.from_domain(universe).currency == currency

SINGLE = """
id: hk_equity
name: 港股 · 龙头
asset_class: us_equity
provider: yahoo
supported_windows: [24h, 7d]
default_window: 24h
instruments:
  - {symbol: "0700", name: 腾讯控股, sector: 通信服务}
  - {symbol: "9988", name: 阿里巴巴, sector: 可选消费}
"""

LIST = """
universes:
  - id: alpha
    name: Alpha
    asset_class: crypto
    provider: coingecko
    supported_windows: [1h, 24h]
    default_window: 1h
    sectors: {BTC: 公链}
  - id: beta
    name: Beta
    asset_class: crypto
    provider: coingecko
    supported_windows: [24h]
    default_window: 24h
"""


def write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_single_mapping_file(tmp_path: Path) -> None:
    write(tmp_path, "hk.yaml", SINGLE)
    repo = YamlUniverseRepository.from_directory(tmp_path)
    universe = repo.get("hk_equity")
    assert len(repo) == 1
    assert universe.name == "港股 · 龙头"
    assert universe.symbols() == ("0700", "9988")
    assert universe.windows[0].value == "24h"


def test_loads_universes_list_and_sorts_by_filename(tmp_path: Path) -> None:
    write(tmp_path, "b_alpha.yaml", LIST)
    write(tmp_path, "a_single.yml", SINGLE)
    repo = YamlUniverseRepository.from_directory(tmp_path)
    assert [universe.id for universe in repo.all()] == ["hk_equity", "alpha", "beta"]


def test_ignores_unrelated_files(tmp_path: Path) -> None:
    write(tmp_path, "hk.yaml", SINGLE)
    write(tmp_path, "notes.md", "# not a universe")
    repo = YamlUniverseRepository.from_directory(tmp_path)
    assert len(repo) == 1


def test_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(UniverseConfigError, match="not found"):
        YamlUniverseRepository.from_directory(tmp_path / "missing")


def test_empty_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(UniverseConfigError, match=r"no \*\.yaml"):
        YamlUniverseRepository.from_directory(tmp_path)


def test_invalid_entry_reports_file_and_index(tmp_path: Path) -> None:
    write(
        tmp_path,
        "bad.yaml",
        "universes:\n  - {id: okay, name: A, asset_class: crypto, provider: p,"
        " supported_windows: [24h], default_window: 24h}\n  - {id: broken}\n",
    )
    with pytest.raises(UniverseConfigError) as excinfo:
        YamlUniverseRepository.from_directory(tmp_path)
    assert "bad.yaml" in str(excinfo.value)
    assert "#1" in str(excinfo.value)


def test_duplicate_id_across_files_raises(tmp_path: Path) -> None:
    write(tmp_path, "one.yaml", SINGLE)
    write(tmp_path, "two.yaml", SINGLE)
    with pytest.raises(UniverseConfigError, match="duplicate universe id"):
        YamlUniverseRepository.from_directory(tmp_path)


def test_non_mapping_entry_raises(tmp_path: Path) -> None:
    write(tmp_path, "list.yaml", "- just-a-string\n")
    with pytest.raises(UniverseConfigError, match="must be a mapping"):
        YamlUniverseRepository.from_directory(tmp_path)


def test_unreadable_yaml_raises(tmp_path: Path) -> None:
    write(tmp_path, "broken.yaml", "id: [unclosed\n")
    with pytest.raises(UniverseConfigError, match="cannot read universe file"):
        YamlUniverseRepository.from_directory(tmp_path)


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    write(
        tmp_path,
        "typo.yaml",
        "id: coins\nname: 币\nasset_class: crypto\nprovider: coingecko\nsupported_window: [24h]\n",
    )
    with pytest.raises(UniverseConfigError) as excinfo:
        YamlUniverseRepository.from_directory(tmp_path)
    assert "supported_window" in str(excinfo.value)


def test_get_is_case_insensitive_and_trims(universe: Universe) -> None:
    repo = YamlUniverseRepository([universe])
    assert repo.get("  EQUITY ").id == "equity"
    with pytest.raises(UniverseNotFound) as excinfo:
        repo.get("nope")
    assert excinfo.value.status == 404
    assert "equity" in (excinfo.value.hint or "")


def test_repository_requires_at_least_one_universe() -> None:
    with pytest.raises(UniverseConfigError, match="no universes configured"):
        YamlUniverseRepository([])
