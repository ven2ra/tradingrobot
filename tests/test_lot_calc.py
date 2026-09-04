from __future__ import annotations

from decimal import Decimal

from trading_robot.strategy.grid_strategy import compute_lots_for_level, _round_down_to_step, _round_up_to_step


def test_lots_floor_to_min_lot(spec):
    # allowed_notional покрывает ровно 2.9 лота по цене -> floor -> 2 лота
    price = Decimal("100.00")
    allowed_notional = price * spec.lot_size * Decimal("2.9")
    lots = compute_lots_for_level(allowed_notional=allowed_notional, price=price, spec=spec)
    assert lots == 2


def test_lots_below_min_lot_is_zero(spec):
    price = Decimal("100.00")
    allowed_notional = price * spec.lot_size * Decimal("0.5")
    lots = compute_lots_for_level(allowed_notional=allowed_notional, price=price, spec=spec)
    assert lots == 0


def test_price_rounds_down_to_step(spec):
    rounded = _round_down_to_step(Decimal("100.017"), spec.price_step)
    assert rounded == Decimal("100.01")


def test_price_rounds_up_to_step(spec):
    rounded = _round_up_to_step(Decimal("100.011"), spec.price_step)
    assert rounded == Decimal("100.02")
