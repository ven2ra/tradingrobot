"""Классификация рыночного режима.

Правила явные и тестируемые, пороги — из конфига (regime.* в YAML).
Никакого ML/чёрного ящика: чистая пороговая логика.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from trading_robot.features.features import Features


class Regime(str, Enum):
    UPTREND = "uptrend"
    DOWNTREND = "downtrend"
    RANGE = "range"
    SHOCK = "shock"
    LOW_LIQUIDITY = "low_liquidity"
    UNCERTAIN = "uncertain"


ENTRY_FORBIDDEN_REGIMES = frozenset({Regime.SHOCK, Regime.LOW_LIQUIDITY, Regime.UNCERTAIN})


@dataclass(frozen=True, slots=True)
class RegimeThresholds:
    # Волатильность (std лог-доходностей) выше которой — шок.
    shock_volatility: Decimal
    # Спред в bps выше которого — низкая ликвидность.
    low_liquidity_spread_bps: Decimal
    # Объём к среднему ниже которого — низкая ликвидность.
    low_liquidity_volume_ratio: Decimal
    # |mid_return| выше которого рынок считается трендовым.
    trend_return_threshold: Decimal
    # Данные старше этого возраста — недостаточно данных для классификации.
    max_age_ms_for_classification: int


def classify_regime(features: Features, thresholds: RegimeThresholds) -> Regime:
    if features.is_stale or features.age_of_data_ms > thresholds.max_age_ms_for_classification:
        return Regime.UNCERTAIN

    if features.volatility >= thresholds.shock_volatility:
        return Regime.SHOCK

    if (
        features.spread_bps >= thresholds.low_liquidity_spread_bps
        or features.volume_vs_avg <= thresholds.low_liquidity_volume_ratio
    ):
        return Regime.LOW_LIQUIDITY

    if features.mid_return >= thresholds.trend_return_threshold:
        return Regime.UPTREND
    if features.mid_return <= -thresholds.trend_return_threshold:
        return Regime.DOWNTREND

    return Regime.RANGE


def entries_allowed(regime: Regime) -> bool:
    return regime not in ENTRY_FORBIDDEN_REGIMES


def grid_allowed(regime: Regime) -> bool:
    """Сетка и усреднение разрешены только в range."""
    return regime == Regime.RANGE
