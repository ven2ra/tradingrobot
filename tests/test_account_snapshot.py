from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from trading_robot.domain.types import LimitOrderRequest, Side, TimeInForce
from trading_robot.brokers.mock_broker import MockBroker
from trading_robot.journal.account_snapshot import build_snapshot
from trading_robot.risk.risk_manager import equity_of


def test_snapshot_includes_only_active_orders(mock_broker: MockBroker, instrument, spec):
    resting = LimitOrderRequest(
        client_order_id="test-snap-resting",
        instrument=instrument,
        side=Side.BUY,
        lots=1,
        price=Decimal("50.00"),  # ниже рынка (mid=100) — не исполнится, остаётся активной
        time_in_force=TimeInForce.DAY,
    )
    filled = LimitOrderRequest(
        client_order_id="test-snap-filled",
        instrument=instrument,
        side=Side.BUY,
        lots=1,
        price=Decimal("110.00"),  # выше рынка на покупку — исполнится сразу
        time_in_force=TimeInForce.DAY,
    )
    mock_broker.place_limit_order(resting)
    mock_broker.place_limit_order(filled)

    account = mock_broker.get_account()
    mark_prices = {instrument.key: Decimal("100.00")}
    specs = {instrument.key: spec}
    equity = equity_of(account, mark_prices, specs)

    snapshot = build_snapshot(account, mark_prices, specs, equity, datetime.now(timezone.utc))

    order_ids = {o.client_order_id for o in snapshot.orders}
    assert order_ids == {"test-snap-resting"}
    resting_snapshot = next(o for o in snapshot.orders if o.client_order_id == "test-snap-resting")
    assert resting_snapshot.status == "accepted"
    assert resting_snapshot.side == "buy"
