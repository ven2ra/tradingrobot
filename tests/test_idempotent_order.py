from __future__ import annotations

from decimal import Decimal

from trading_robot.domain.types import LimitOrderRequest, Side, TimeInForce
from trading_robot.brokers.mock_broker import MockBroker


def test_place_limit_order_is_idempotent(mock_broker: MockBroker, instrument):
    request = LimitOrderRequest(
        client_order_id="GRID-fixed-key-buy-1",
        instrument=instrument,
        side=Side.BUY,
        lots=1,
        price=Decimal("50.00"),  # заведомо ниже рынка, не исполнится сразу
        time_in_force=TimeInForce.DAY,
    )

    ack1 = mock_broker.place_limit_order(request)
    ack2 = mock_broker.place_limit_order(request)

    assert ack1.broker_order_id == ack2.broker_order_id
    assert ack1.client_order_id == ack2.client_order_id

    account = mock_broker.get_account()
    matching_orders = [o for o in account.orders if o.client_order_id == request.client_order_id]
    assert len(matching_orders) == 1  # повторный такт не породил вторую заявку
