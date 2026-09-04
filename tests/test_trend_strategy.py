from __future__ import annotations

from decimal import Decimal

from trading_robot.domain.types import Position, Side
from trading_robot.regime.regime import Regime
from trading_robot.strategy.trend_strategy import (
    TrendConfig,
    build_trend_entry,
    build_trend_exit,
    compute_trend_entry_lots,
)


def _trend_config() -> TrendConfig:
    return TrendConfig(
        entry_notional_fraction=Decimal("0.15"),
        aggressive_offset_bps=Decimal("15"),
        stop_loss_pct=Decimal("0.015"),
        take_profit_pct=Decimal("0.03"),
    )


_CONFIRMED_VOLUME = Decimal("2.0")  # выше min_volume_ratio=1.3 по умолчанию


def test_trend_entry_only_in_trend_regimes(instrument):
    level, _ = build_trend_entry(
        instrument=instrument, regime=Regime.RANGE, mid_price=Decimal("100"),
        volume_vs_avg=_CONFIRMED_VOLUME,
        current_inventory_lots=0, config=_trend_config(), tick_bucket="2026-09-04",
    )
    assert level is None


def test_trend_entry_uptrend_buys_above_mid(instrument):
    level, reason = build_trend_entry(
        instrument=instrument, regime=Regime.UPTREND, mid_price=Decimal("100"),
        volume_vs_avg=_CONFIRMED_VOLUME,
        current_inventory_lots=0, config=_trend_config(), tick_bucket="2026-09-04",
    )
    assert level is not None
    assert level.side == Side.BUY
    assert level.price > Decimal("100")  # агрессивная лимитка пересекает спред вверх
    assert "momentum entry" in reason


def test_trend_entry_downtrend_sells_below_mid(instrument):
    level, _ = build_trend_entry(
        instrument=instrument, regime=Regime.DOWNTREND, mid_price=Decimal("100"),
        volume_vs_avg=_CONFIRMED_VOLUME,
        current_inventory_lots=0, config=_trend_config(), tick_bucket="2026-09-04",
    )
    assert level is not None
    assert level.side == Side.SELL
    assert level.price < Decimal("100")


def test_trend_entry_no_averaging(instrument):
    level, reason = build_trend_entry(
        instrument=instrument, regime=Regime.UPTREND, mid_price=Decimal("100"),
        volume_vs_avg=_CONFIRMED_VOLUME,
        current_inventory_lots=5, config=_trend_config(), tick_bucket="2026-09-04",
    )
    assert level is None
    assert "no averaging" in reason


def test_trend_entry_idempotent_client_order_id(instrument):
    level1, _ = build_trend_entry(
        instrument=instrument, regime=Regime.UPTREND, mid_price=Decimal("100"),
        volume_vs_avg=_CONFIRMED_VOLUME,
        current_inventory_lots=0, config=_trend_config(), tick_bucket="2026-09-04",
    )
    level2, _ = build_trend_entry(
        instrument=instrument, regime=Regime.UPTREND, mid_price=Decimal("101"),
        volume_vs_avg=_CONFIRMED_VOLUME,
        current_inventory_lots=0, config=_trend_config(), tick_bucket="2026-09-04",
    )
    assert level1.client_order_id == level2.client_order_id  # тот же тикер+тренд+день


def test_trend_entry_rejected_without_volume_confirmation(instrument):
    level, reason = build_trend_entry(
        instrument=instrument, regime=Regime.UPTREND, mid_price=Decimal("100"),
        volume_vs_avg=Decimal("1.1"),  # ниже min_volume_ratio=1.3 по умолчанию
        current_inventory_lots=0, config=_trend_config(), tick_bucket="2026-09-04",
    )
    assert level is None
    assert "volume not confirmed" in reason


def test_trend_entry_allowed_exactly_at_volume_threshold(instrument):
    config = _trend_config()
    level, reason = build_trend_entry(
        instrument=instrument, regime=Regime.UPTREND, mid_price=Decimal("100"),
        volume_vs_avg=config.min_volume_ratio,  # ровно на границе — должно пропускать
        current_inventory_lots=0, config=config, tick_bucket="2026-09-04",
    )
    assert level is not None


def test_trend_exit_long_stop_loss(instrument):
    position = Position(instrument=instrument, lots=10, average_price=Decimal("100"))
    level, reason = build_trend_exit(
        instrument=instrument, position=position, mark_price=Decimal("98"),  # -2%, стоп -1.5%
        config=_trend_config(), tick_bucket="2026-09-04",
    )
    assert level is not None
    assert level.side == Side.SELL
    assert level.purpose == "exit_stop_loss"
    assert "stop-loss" in reason


def test_trend_exit_long_take_profit(instrument):
    position = Position(instrument=instrument, lots=10, average_price=Decimal("100"))
    level, reason = build_trend_exit(
        instrument=instrument, position=position, mark_price=Decimal("104"),  # +4%, тейк +3%
        config=_trend_config(), tick_bucket="2026-09-04",
    )
    assert level is not None
    assert level.side == Side.SELL
    assert level.purpose == "exit_take_profit"


def test_trend_exit_long_holding_between_thresholds(instrument):
    position = Position(instrument=instrument, lots=10, average_price=Decimal("100"))
    level, reason = build_trend_exit(
        instrument=instrument, position=position, mark_price=Decimal("101"),  # +1%, между стопом и тейком
        config=_trend_config(), tick_bucket="2026-09-04",
    )
    assert level is None
    assert "holding" in reason


def test_trend_exit_short_stop_loss(instrument):
    position = Position(instrument=instrument, lots=-10, average_price=Decimal("100"))
    level, reason = build_trend_exit(
        instrument=instrument, position=position, mark_price=Decimal("102"),  # +2% против шорта, стоп -1.5%(в его сторону)
        config=_trend_config(), tick_bucket="2026-09-04",
    )
    assert level is not None
    assert level.side == Side.BUY
    assert level.purpose == "exit_stop_loss"


def test_trend_exit_short_take_profit(instrument):
    position = Position(instrument=instrument, lots=-10, average_price=Decimal("100"))
    level, reason = build_trend_exit(
        instrument=instrument, position=position, mark_price=Decimal("96"),  # -4%, тейк -3%
        config=_trend_config(), tick_bucket="2026-09-04",
    )
    assert level is not None
    assert level.side == Side.BUY
    assert level.purpose == "exit_take_profit"


def test_trend_exit_no_position(instrument):
    position = Position(instrument=instrument, lots=0, average_price=Decimal("100"))
    level, reason = build_trend_exit(
        instrument=instrument, position=position, mark_price=Decimal("100"),
        config=_trend_config(), tick_bucket="2026-09-04",
    )
    assert level is None
    assert "no position" in reason


def test_compute_trend_entry_lots():
    lots = compute_trend_entry_lots(allowed_notional=Decimal("10000"), price=Decimal("100"), lot_size=10)
    assert lots == 10  # floor(10000 / (100*10))


def test_compute_trend_entry_lots_zero_when_too_small():
    lots = compute_trend_entry_lots(allowed_notional=Decimal("500"), price=Decimal("100"), lot_size=10)
    assert lots == 0  # 500 / 1000 < 1 лот
