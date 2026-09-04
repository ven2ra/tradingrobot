"""Доменные типы торгового робота.

Все денежные величины, цены и количества — Decimal. float запрещён
для этих полей во избежание ошибок округления.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"


class OrderStatus(str, Enum):
    NEW = "new"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class InstrumentClass(str, Enum):
    SHARE = "share"
    BOND = "bond"
    FUTURE = "future"


@dataclass(frozen=True, slots=True)
class Instrument:
    """Идентификатор инструмента.

    ticker — человекочитаемый код (например SBER, RIZ5).
    board — секция торгов (TQBR, FORTS, ...).
    expiration — дата экспирации для фьючерсов, иначе None.
    """

    ticker: str
    board: str
    instrument_class: InstrumentClass
    expiration: datetime | None = None

    @property
    def key(self) -> str:
        return f"{self.board}:{self.ticker}"


@dataclass(frozen=True, slots=True)
class Quote:
    instrument: Instrument
    bid: Decimal
    ask: Decimal
    last: Decimal
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    price: Decimal
    lots: int


@dataclass(frozen=True, slots=True)
class OrderBook:
    instrument: Instrument
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class Bar:
    instrument: Instrument
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    instrument: Instrument
    lot_size: int
    price_step: Decimal
    currency: str
    # Гарантийное обеспечение за один лот (только FORTS). None для акций/облигаций.
    initial_margin_per_lot: Decimal | None = None
    min_price_increment_value: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Position:
    instrument: Instrument
    lots: int
    average_price: Decimal


@dataclass(frozen=True, slots=True)
class Order:
    client_order_id: str
    broker_order_id: str | None
    instrument: Instrument
    side: Side
    lots: int
    price: Decimal
    time_in_force: TimeInForce
    status: OrderStatus
    filled_lots: int = 0


@dataclass(frozen=True, slots=True)
class AccountState:
    cash: Decimal
    currency: str
    positions: tuple[Position, ...]
    orders: tuple[Order, ...]
    # Использованное ГО по срочному рынку (сумма по всем позициям FORTS). None если брокер не отдаёт.
    used_margin: Decimal | None = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass(frozen=True, slots=True)
class LimitOrderRequest:
    client_order_id: str
    instrument: Instrument
    side: Side
    lots: int
    price: Decimal
    time_in_force: TimeInForce = TimeInForce.DAY


@dataclass(frozen=True, slots=True)
class OrderAck:
    client_order_id: str
    broker_order_id: str | None
    status: OrderStatus
    message: str = ""
