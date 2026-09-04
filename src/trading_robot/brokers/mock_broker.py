"""Детерминированный брокер для paper-режима и тестов.

Не является приближением реального стакана биржи — это управляемая
симуляция с фиксированным ГПСЧ (seed из конфига), которая:
  * ведёт собственный синтетический стакан вокруг референсной цены;
  * исполняет лимитные заявки при пересечении цены с проскальзыванием
    по правилам из конфига (bps от цены, не выдумка "как повезёт");
  * хранит позиции/деньги/заявки в памяти.

Реальный рыночный риск НЕ моделируется (нет геп-рисков, нет частичных
исполнений сверх заданных правил) — это инструмент для отладки логики
робота, а не для оценки прибыльности стратегии.
"""
from __future__ import annotations

import random
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from trading_robot.data.liquid_tickers import lot_size_for
from trading_robot.domain.types import (
    AccountState,
    Bar,
    Instrument,
    InstrumentSpec,
    LimitOrderRequest,
    Order,
    OrderAck,
    OrderBook,
    OrderBookLevel,
    OrderStatus,
    Position,
    Quote,
    Side,
    TimeInForce,
)


class MockBrokerError(Exception):
    pass


class MockBroker:
    """Paper-брокер. Удовлетворяет Protocol BrokerAdapter (см. interfaces/broker.py)."""

    def __init__(
        self,
        specs: dict[str, InstrumentSpec],
        initial_cash: Decimal,
        currency: str = "RUB",
        seed: int = 42,
        slippage_bps: Decimal = Decimal("2"),
        base_prices: dict[str, Decimal] | None = None,
    ) -> None:
        self._specs = specs
        self._rng = random.Random(seed)
        self._slippage_bps = slippage_bps
        self._connected = False
        self._cash = initial_cash
        self._currency = currency
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}  # client_order_id -> Order
        self._broker_id_seq = 0
        self._prices: dict[str, Decimal] = dict(base_prices or {})
        for key, spec in specs.items():
            self._prices.setdefault(key, Decimal("100"))

    # -- connection ---------------------------------------------------
    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def _require_connected(self) -> None:
        if not self._connected:
            raise MockBrokerError("broker not connected")

    def _ensure_spec(self, instrument: Instrument) -> InstrumentSpec:
        """Регистрирует DEFAULT-спецификацию для тикера, которого не было в
        конфиге при старте (например добавлен через веб-панель после запуска) —
        лот берётся из курируемого списка liquid_tickers.py, если тикер там
        есть, иначе DEFAULT=10. Реальный брокер такой проблемы не имеет —
        get_instrument_spec там всегда бьёт в API брокера.
        """
        key = instrument.key
        spec = self._specs.get(key)
        if spec is not None:
            return spec
        spec = InstrumentSpec(
            instrument=instrument,
            lot_size=lot_size_for(instrument.ticker),
            price_step=Decimal("0.01"),
            currency=self._currency,
        )
        self._specs[key] = spec
        self._prices.setdefault(key, Decimal("100"))
        return spec

    # -- market data ----------------------------------------------------
    def _walk_price(self, key: str) -> Decimal:
        spec = self._specs[key]
        price = self._prices[key]
        # Небольшое случайное блуждание в пределах шага цены, детерминировано seed'ом.
        steps = self._rng.choice([-2, -1, 0, 0, 1, 2])
        new_price = price + spec.price_step * steps
        if new_price <= 0:
            new_price = price
        self._prices[key] = new_price
        return new_price

    def get_quote(self, instrument: Instrument) -> Quote:
        self._require_connected()
        key = instrument.key
        spec = self._ensure_spec(instrument)
        mid = self._walk_price(key)
        half_spread = spec.price_step
        bid = self._round_to_step(mid - half_spread, spec.price_step)
        ask = self._round_to_step(mid + half_spread, spec.price_step)
        return Quote(
            instrument=instrument,
            bid=bid,
            ask=ask,
            last=mid,
            timestamp=datetime.now(timezone.utc),
        )

    def get_orderbook(self, instrument: Instrument, depth: int) -> OrderBook:
        self._require_connected()
        key = instrument.key
        spec = self._ensure_spec(instrument)
        mid = self._prices[key]
        bids = tuple(
            OrderBookLevel(
                price=self._round_to_step(mid - spec.price_step * (i + 1), spec.price_step),
                lots=10 + i,
            )
            for i in range(depth)
        )
        asks = tuple(
            OrderBookLevel(
                price=self._round_to_step(mid + spec.price_step * (i + 1), spec.price_step),
                lots=10 + i,
            )
            for i in range(depth)
        )
        return OrderBook(instrument=instrument, bids=bids, asks=asks, timestamp=datetime.now(timezone.utc))

    def get_bars(self, instrument: Instrument, interval: str, limit: int) -> list[Bar]:
        self._require_connected()
        key = instrument.key
        spec = self._ensure_spec(instrument)
        bars: list[Bar] = []
        price = self._prices[key]
        for i in range(limit):
            o = price
            h = o + spec.price_step
            l = o - spec.price_step
            c = self._walk_price(key)
            bars.append(
                Bar(
                    instrument=instrument,
                    open=o,
                    high=max(h, c),
                    low=min(l, c),
                    close=c,
                    volume=1000 + self._rng.randint(0, 500),
                    timestamp=datetime.now(timezone.utc),
                )
            )
            price = c
        return bars

    def get_instrument_spec(self, instrument: Instrument) -> InstrumentSpec:
        return self._ensure_spec(instrument)

    @staticmethod
    def _round_to_step(price: Decimal, step: Decimal) -> Decimal:
        if step <= 0:
            return price
        steps = (price / step).to_integral_value(rounding=ROUND_HALF_UP)
        return steps * step

    # -- account ----------------------------------------------------------
    def get_account(self) -> AccountState:
        return AccountState(
            cash=self._cash,
            currency=self._currency,
            positions=tuple(self._positions.values()),
            orders=tuple(self._orders.values()),
            used_margin=self._used_margin(),
            timestamp=datetime.now(timezone.utc),
        )

    def sync_state(self) -> AccountState:
        # У MockBroker состояние уже "живёт" в процессе — синхронизация тривиальна.
        return self.get_account()

    def _used_margin(self) -> Decimal | None:
        total = Decimal("0")
        has_margin = False
        for key, pos in self._positions.items():
            spec = self._specs.get(key)
            if spec and spec.initial_margin_per_lot is not None:
                has_margin = True
                total += spec.initial_margin_per_lot * abs(pos.lots)
        return total if has_margin else None

    # -- orders -------------------------------------------------------------
    def place_limit_order(self, request: LimitOrderRequest) -> OrderAck:
        self._require_connected()
        # Идемпотентность: повтор того же client_order_id не создаёт вторую заявку.
        existing = self._orders.get(request.client_order_id)
        if existing is not None:
            return OrderAck(
                client_order_id=existing.client_order_id,
                broker_order_id=existing.broker_order_id,
                status=existing.status,
                message="idempotent replay: order already exists",
            )

        self._broker_id_seq += 1
        broker_order_id = f"MOCK-{self._broker_id_seq}"
        order = Order(
            client_order_id=request.client_order_id,
            broker_order_id=broker_order_id,
            instrument=request.instrument,
            side=request.side,
            lots=request.lots,
            price=request.price,
            time_in_force=request.time_in_force,
            status=OrderStatus.ACCEPTED,
        )
        self._orders[request.client_order_id] = order
        self._try_fill(order)
        return OrderAck(
            client_order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            status=self._orders[request.client_order_id].status,
        )

    def cancel_order(self, broker_order_id: str) -> None:
        self._require_connected()
        for cid, order in list(self._orders.items()):
            if order.broker_order_id == broker_order_id and order.status in (
                OrderStatus.NEW,
                OrderStatus.ACCEPTED,
                OrderStatus.PARTIALLY_FILLED,
            ):
                self._orders[cid] = replace(order, status=OrderStatus.CANCELLED)

    def _try_fill(self, order: Order) -> None:
        """Исполняет лимитку немедленно, если цена пересекает текущий mid + slippage."""
        key = order.instrument.key
        spec = self._ensure_spec(order.instrument)
        mid = self._prices[key]
        slip = mid * self._slippage_bps / Decimal("10000")

        crosses = (
            order.side == Side.BUY and order.price >= mid - slip
        ) or (order.side == Side.SELL and order.price <= mid + slip)
        if not crosses:
            return

        fill_price = order.price
        notional = fill_price * spec.lot_size * order.lots
        if order.side == Side.BUY:
            if notional > self._cash:
                self._orders[order.client_order_id] = replace(order, status=OrderStatus.REJECTED)
                return
            self._cash -= notional
            self._apply_position_delta(key, order.instrument, order.lots, fill_price)
        else:
            self._cash += notional
            self._apply_position_delta(key, order.instrument, -order.lots, fill_price)

        self._orders[order.client_order_id] = replace(
            order, status=OrderStatus.FILLED, filled_lots=order.lots
        )

    def _apply_position_delta(self, key: str, instrument: Instrument, lots_delta: int, price: Decimal) -> None:
        existing = self._positions.get(key)
        if existing is None:
            self._positions[key] = Position(instrument=instrument, lots=lots_delta, average_price=price)
            return
        new_lots = existing.lots + lots_delta
        if new_lots == 0:
            del self._positions[key]
            return
        same_direction = (existing.lots >= 0 and lots_delta > 0) or (existing.lots <= 0 and lots_delta < 0)
        flipped_sign = (existing.lots > 0 and new_lots < 0) or (existing.lots < 0 and new_lots > 0)
        if same_direction:
            total_cost = existing.average_price * existing.lots + price * lots_delta
            new_avg = total_cost / new_lots
        elif flipped_sign:
            # Сделка "пробила" через ноль: старая позиция (long или short)
            # полностью закрыта этой же сделкой, а остаток лотов открывает
            # НОВУЮ позицию в противоположную сторону — её средняя цена
            # входа это цена текущей сделки, а не средняя цена закрытой
            # позиции (иначе P&L новой позиции считался бы от чужой цены).
            new_avg = price
        else:
            # Частичное сокращение позиции без разворота — средняя цена
            # входа остающейся части не меняется.
            new_avg = existing.average_price
        self._positions[key] = Position(instrument=instrument, lots=new_lots, average_price=new_avg)

    @staticmethod
    def new_client_order_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"
