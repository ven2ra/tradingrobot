from __future__ import annotations

from decimal import Decimal

from trading_robot.regime.regime import Regime
from trading_robot.strategy.grid_strategy import GridConfig, build_grid_plan


def _grid_config() -> GridConfig:
    return GridConfig(
        max_levels=3,
        max_inventory_lots=10,
        step_volatility_multiplier=Decimal("3"),
        min_step_price_steps=2,
        level_notional_fraction=Decimal("0.1"),
    )


def test_grid_forbidden_in_uptrend(instrument, spec):
    plan = build_grid_plan(
        instrument=instrument,
        spec=spec,
        mid_price=Decimal("100"),
        volatility=Decimal("0.001"),
        regime=Regime.UPTREND,
        current_inventory_lots=0,
        trend_direction=None,
        config=_grid_config(),
        tick_bucket="2026-09-04",
    )
    assert plan.levels == []


def test_grid_forbidden_in_downtrend(instrument, spec):
    plan = build_grid_plan(
        instrument=instrument,
        spec=spec,
        mid_price=Decimal("100"),
        volatility=Decimal("0.001"),
        regime=Regime.DOWNTREND,
        current_inventory_lots=0,
        trend_direction=None,
        config=_grid_config(),
        tick_bucket="2026-09-04",
    )
    assert plan.levels == []


def test_grid_forbidden_in_shock(instrument, spec):
    plan = build_grid_plan(
        instrument=instrument,
        spec=spec,
        mid_price=Decimal("100"),
        volatility=Decimal("0.05"),
        regime=Regime.SHOCK,
        current_inventory_lots=0,
        trend_direction=None,
        config=_grid_config(),
        tick_bucket="2026-09-04",
    )
    assert plan.levels == []


def test_grid_allowed_in_range(instrument, spec):
    plan = build_grid_plan(
        instrument=instrument,
        spec=spec,
        mid_price=Decimal("100"),
        volatility=Decimal("0.001"),
        regime=Regime.RANGE,
        current_inventory_lots=0,
        trend_direction=None,
        config=_grid_config(),
        tick_bucket="2026-09-04",
    )
    assert len(plan.levels) > 0


def test_grid_no_buy_against_downtrend_inventory(instrument, spec):
    plan = build_grid_plan(
        instrument=instrument,
        spec=spec,
        mid_price=Decimal("100"),
        volatility=Decimal("0.001"),
        regime=Regime.RANGE,
        current_inventory_lots=5,
        trend_direction=Regime.DOWNTREND,
        config=_grid_config(),
        tick_bucket="2026-09-04",
    )
    from trading_robot.domain.types import Side

    assert all(level.side != Side.BUY for level in plan.levels)
