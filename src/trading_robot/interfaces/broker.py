"""Интерфейс BrokerAdapter.

Стратегия, риск и режим НЕ импортируют конкретных брокеров — только этот
Protocol. Любая реализация (MockBroker, TInvestAdapter, ...) обязана
удовлетворять данному контракту.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from trading_robot.domain.types import (
    AccountState,
    Bar,
    Instrument,
    InstrumentSpec,
    LimitOrderRequest,
    OrderAck,
    OrderBook,
    Quote,
)


@runtime_checkable
class BrokerAdapter(Protocol):
    def connect(self) -> None:
        ...

    def disconnect(self) -> None:
        ...

    def is_connected(self) -> bool:
        ...

    def get_quote(self, instrument: Instrument) -> Quote:
        ...

    def get_orderbook(self, instrument: Instrument, depth: int) -> OrderBook:
        ...

    def get_bars(self, instrument: Instrument, interval: str, limit: int) -> list[Bar]:
        ...

    def get_instrument_spec(self, instrument: Instrument) -> InstrumentSpec:
        ...

    def get_account(self) -> AccountState:
        ...

    def place_limit_order(self, request: LimitOrderRequest) -> OrderAck:
        ...

    def cancel_order(self, broker_order_id: str) -> None:
        ...

    def sync_state(self) -> AccountState:
        ...
