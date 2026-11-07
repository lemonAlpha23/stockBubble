"""板块分类的守门测试：口径必须与 TradingView 对齐，且没有遗漏。"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from stockbubble.adapters.universes import YamlUniverseRepository
from stockbubble.domain.models import Universe

CONFIG = Path(__file__).resolve().parents[1] / "config" / "universes"

# TradingView / Morningstar 的 11 个板块（zh-CN 文案）
US_SECTORS = {
    "科技",
    "金融服务",
    "医疗保健",
    "周期性消费",
    "防御性消费",
    "通信服务",
    "工业",
    "能源",
    "基础材料",
    "公用事业",
    "房地产",
}

# TradingView 加密板块口径
CRYPTO_SECTORS = {
    "第一层",
    "第二层",
    "比特币生态",
    "DeFi",
    "交易所",
    "Meme",
    "AI",
    "游戏",
    "RWA",
    "稳定币",
    "代币化股票",
    "预言机",
    "隐私",
    "支付",
    "存储",
    "基础设施",
    "其他",
}


@pytest.fixture(scope="module")
def repo() -> YamlUniverseRepository:
    return YamlUniverseRepository.from_directory(CONFIG)


def test_us_universe_uses_morningstar_sectors(repo: YamlUniverseRepository) -> None:
    universe = repo.get("us_equity")
    sectors = {item.sector for item in universe.instruments}

    assert sectors == US_SECTORS
    assert all(item.sector for item in universe.instruments)


def test_us_sector_sizes_are_plausible(repo: YamlUniverseRepository) -> None:
    universe = repo.get("us_equity")
    counts = Counter(item.sector for item in universe.instruments)

    # 每块至少几只，且没有单块吃掉一半
    assert min(counts.values()) >= 10
    assert max(counts.values()) <= len(universe.instruments) / 3


def test_crypto_sectors_use_tradingview_taxonomy(repo: YamlUniverseRepository) -> None:
    universe = repo.get("crypto")
    values = set(universe.sectors.values())

    assert values <= CRYPTO_SECTORS
    assert universe.fallback_sector == "其他"


def test_crypto_sector_map_is_uppercase_and_dense(repo: YamlUniverseRepository) -> None:
    universe = repo.get("crypto")

    assert all(key == key.upper() for key in universe.sectors)
    assert len(universe.sectors) >= 200


def test_unknown_symbol_falls_back_to_sector(repo: YamlUniverseRepository) -> None:
    universe = repo.get("crypto")

    assert universe.sector_of("TOTALLY_NEW_COIN") == "其他"
    assert universe.sector_of("btc") == "第一层"


def test_sector_lookup_is_case_insensitive(repo: YamlUniverseRepository) -> None:
    us = repo.get("us_equity")

    assert us.sector_of("aapl") == "科技"


def test_universe_without_fallback_returns_none() -> None:
    universe = Universe.model_validate(
        {
            "id": "probe",
            "name": "probe",
            "asset_class": "crypto",
            "provider": "coingecko",
        }
    )

    assert universe.sector_of("WHATEVER") is None
