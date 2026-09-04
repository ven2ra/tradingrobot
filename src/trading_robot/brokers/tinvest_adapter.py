"""Каркас адаптера для T-Invest API (официальный SDK `tinkoff-investments`,
пакет `tinkoff.invest`).

Документация: https://tinkoff.github.io/investApi/
Репозиторий SDK: https://github.com/RussianInvestments/invest-python

ВАЖНО: этот файл сознательно НЕ вызывает реальные gRPC-методы. Точные
имена методов, полей ответа и enum'ов SDK здесь не гарантированы без
сверки с актуальной версией пакета на момент интеграции — вместо
угадывания везде стоит `NotImplementedError` с указанием, какой
официальный вызов нужно подставить.

Секреты: токен читается ТОЛЬКО из переменной окружения
`TINVEST_TOKEN` (или другой, заданной в конфиге `broker.token_env`).
В коде и YAML — только имя переменной, никогда не сам токен.

Контуры:
  * INVEST_GRPC_API          — боевой контур (реальные деньги).
  * INVEST_GRPC_API_SANDBOX  — песочница брокера (не наш internal paper-режим
    MockBroker, а официальная песочница T-Invest; можно использовать вместо
    MockBroker для более реалистичного paper-тестирования на этапе интеграции).
"""
from __future__ import annotations

import os
from decimal import Decimal

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


class TInvestConfigError(Exception):
    pass


class TInvestAdapter:
    """Тонкий перевод доменных типов робота <-> SDK tinkoff.invest.

    Не смешивать с методами других брокеров (ALOR/Finam/BCS) — при смене
    брокера пишется отдельный адаптер, реализующий тот же Protocol
    BrokerAdapter.
    """

    def __init__(self, token_env: str = "TINVEST_TOKEN", account_id_env: str = "TINVEST_ACCOUNT_ID", sandbox: bool = True) -> None:
        self._token_env = token_env
        self._account_id_env = account_id_env
        self._sandbox = sandbox
        self._client = None  # type: ignore[var-annotated]
        self._connected = False

    def _read_token(self) -> str:
        token = os.environ.get(self._token_env)
        if not token:
            raise TInvestConfigError(
                f"переменная окружения {self._token_env} не задана — токен T-Invest обязателен"
            )
        return token

    def connect(self) -> None:
        # TODO: подставить реальную инициализацию клиента SDK, например:
        #   from tinkoff.invest import Client
        #   from tinkoff.invest.constants import INVEST_GRPC_API_SANDBOX, INVEST_GRPC_API
        #   target = INVEST_GRPC_API_SANDBOX if self._sandbox else INVEST_GRPC_API
        #   self._client = Client(self._read_token(), target=target).__enter__()
        # Сверить точный способ управления контекстным менеджером Client
        # с актуальной версией пакета tinkoff-investments перед реализацией.
        raise NotImplementedError(
            "TInvestAdapter.connect: подставить инициализацию tinkoff.invest.Client "
            "(см. https://github.com/RussianInvestments/invest-python)"
        )

    def disconnect(self) -> None:
        raise NotImplementedError("TInvestAdapter.disconnect: закрыть Client (__exit__)")

    def is_connected(self) -> bool:
        return self._connected

    def get_quote(self, instrument: Instrument) -> Quote:
        # TODO: MarketDataService.get_last_prices / get_order_book (best bid/ask)
        # см. tinkoff.invest.services.MarketDataService
        raise NotImplementedError(
            "TInvestAdapter.get_quote: подставить вызов MarketDataService "
            "(get_last_prices / get_order_book) официального SDK"
        )

    def get_orderbook(self, instrument: Instrument, depth: int) -> OrderBook:
        # TODO: MarketDataService.get_order_book(figi=..., depth=depth)
        raise NotImplementedError(
            "TInvestAdapter.get_orderbook: подставить MarketDataService.get_order_book"
        )

    def get_bars(self, instrument: Instrument, interval: str, limit: int) -> list[Bar]:
        # TODO: MarketDataService.get_candles(figi=..., interval=CandleInterval.*, from_=..., to=...)
        raise NotImplementedError(
            "TInvestAdapter.get_bars: подставить MarketDataService.get_candles"
        )

    def get_instrument_spec(self, instrument: Instrument) -> InstrumentSpec:
        # TODO: InstrumentsService.share/bond/future(...) -> lot, min_price_increment
        # Для FORTS: FuturesService / margin через InstrumentsService.get_futures_margin
        raise NotImplementedError(
            "TInvestAdapter.get_instrument_spec: подставить InstrumentsService.* "
            "(конкретный метод зависит от instrument.instrument_class)"
        )

    def get_account(self) -> AccountState:
        # TODO: UsersService.get_accounts + OperationsService.get_portfolio +
        # OperationsService.get_positions + OrdersService.get_orders
        raise NotImplementedError(
            "TInvestAdapter.get_account: подставить UsersService/OperationsService/OrdersService"
        )

    def place_limit_order(self, request: LimitOrderRequest) -> OrderAck:
        # TODO: OrdersService.post_order(
        #   figi=..., quantity=request.lots, price=quotation_from_decimal(request.price),
        #   direction=ORDER_DIRECTION_BUY/SELL, account_id=..., order_type=ORDER_TYPE_LIMIT,
        #   order_id=request.client_order_id,  # используем как идемпотентный ключ
        # )
        raise NotImplementedError(
            "TInvestAdapter.place_limit_order: подставить OrdersService.post_order, "
            "order_id=request.client_order_id для идемпотентности"
        )

    def cancel_order(self, broker_order_id: str) -> None:
        # TODO: OrdersService.cancel_order(account_id=..., order_id=broker_order_id)
        raise NotImplementedError("TInvestAdapter.cancel_order: подставить OrdersService.cancel_order")

    def sync_state(self) -> AccountState:
        return self.get_account()
