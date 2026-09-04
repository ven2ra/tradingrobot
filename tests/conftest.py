from __future__ import annotations

from decimal import Decimal

import pytest

from trading_robot.domain.types import Instrument, InstrumentClass, InstrumentSpec
from trading_robot.brokers.mock_broker import MockBroker


@pytest.fixture
def instrument() -> Instrument:
    return Instrument(ticker="SBER", board="TQBR", instrument_class=InstrumentClass.SHARE)


@pytest.fixture
def spec(instrument: Instrument) -> InstrumentSpec:
    return InstrumentSpec(
        instrument=instrument,
        lot_size=10,
        price_step=Decimal("0.01"),
        currency="RUB",
    )


@pytest.fixture
def mock_broker(instrument: Instrument, spec: InstrumentSpec) -> MockBroker:
    broker = MockBroker(
        specs={instrument.key: spec},
        initial_cash=Decimal("1000000"),
        seed=7,
        base_prices={instrument.key: Decimal("100.00")},
    )
    broker.connect()
    return broker
