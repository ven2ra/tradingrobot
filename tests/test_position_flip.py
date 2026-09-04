from __future__ import annotations

from decimal import Decimal

from trading_robot.domain.types import LimitOrderRequest, Side, TimeInForce
from trading_robot.brokers.mock_broker import MockBroker


def test_position_flip_through_zero_resets_average_price(mock_broker: MockBroker, instrument):
    """Long 3 lots @110, затем sell 8 lots @90 (mid=100, обе заявки исполняются
    сразу): старая long-позиция полностью закрывается, а оставшиеся 5 лотов
    открывают НОВУЮ short-позицию. Её средняя цена входа должна быть ценой
    этой сделки (90), а не унаследованной от закрытой long-позиции (110).
    """
    buy = LimitOrderRequest(
        client_order_id="test-flip-buy-1",
        instrument=instrument,
        side=Side.BUY,
        lots=3,
        price=Decimal("110.00"),
        time_in_force=TimeInForce.DAY,
    )
    mock_broker.place_limit_order(buy)

    account = mock_broker.get_account()
    pos = next(p for p in account.positions if p.instrument.key == instrument.key)
    assert pos.lots == 3
    assert pos.average_price == Decimal("110.00")

    sell = LimitOrderRequest(
        client_order_id="test-flip-sell-1",
        instrument=instrument,
        side=Side.SELL,
        lots=8,
        price=Decimal("90.00"),
        time_in_force=TimeInForce.DAY,
    )
    mock_broker.place_limit_order(sell)

    account = mock_broker.get_account()
    pos = next(p for p in account.positions if p.instrument.key == instrument.key)
    assert pos.lots == -5
    assert pos.average_price == Decimal("90.00")


def test_partial_reduction_keeps_average_price(mock_broker: MockBroker, instrument):
    """Long 10 лотов @110, затем sell 4 лота @90 (частичное сокращение, БЕЗ
    разворота знака) — средняя цена оставшихся 6 лотов не должна меняться."""
    buy = LimitOrderRequest(
        client_order_id="test-partial-buy-1",
        instrument=instrument,
        side=Side.BUY,
        lots=10,
        price=Decimal("110.00"),
        time_in_force=TimeInForce.DAY,
    )
    mock_broker.place_limit_order(buy)

    sell = LimitOrderRequest(
        client_order_id="test-partial-sell-1",
        instrument=instrument,
        side=Side.SELL,
        lots=4,
        price=Decimal("90.00"),
        time_in_force=TimeInForce.DAY,
    )
    mock_broker.place_limit_order(sell)

    account = mock_broker.get_account()
    pos = next(p for p in account.positions if p.instrument.key == instrument.key)
    assert pos.lots == 6
    assert pos.average_price == Decimal("110.00")
