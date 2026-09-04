"""Адаптер для T-Invest API — официальный SDK T-Банка.

ВАЖНО (изменилось после первой версии этого файла): пакет
`tinkoff-investments` (модуль `tinkoff.invest`,
github.com/RussianInvestments/invest-python) СНЯТ с PyPI, а сам
GitHub-репозиторий переведён в архив ("Public archive") — Т-Банк
(бывш. Тинькофф) перенёс разработку на собственный GitLab и
переименовал пакет. Актуально на момент написания:

  * пакет:  `t-tech-investments`
  * модуль: `t_tech.invest` (было `tinkoff.invest`)
  * установка (свой индекс PyPI, обычный `pip install t-tech-investments`
    без --index-url не находит пакет):
        pip install t-tech-investments \
          --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple
  * репозиторий: https://opensource.tbank.ru/invest/invest-python

Все имена классов/enum/методов ниже проверены живым импортом
установленного пакета (`t_tech.invest.services.Services`,
`inspect.signature` на каждом использованном методе) — сигнатуры
совпадают 1-в-1 с тем, что было в архивном `tinkoff.invest` (это
переименованный форк, не переписанный заново). Единственное, что
здесь не проверено «на живую» — реальный ответ от боевого/sandbox
gRPC с настоящим счётом (нужен токен и сетевой доступ с конкретного
сервера). Перед боевым использованием прогоните smoke-test
(trading.mode: paper, broker.kind: tinvest, broker.sandbox: true) и
сверьте результат с личным кабинетом.

Контуры (хосты тоже сменились с *.tinkoff.ru на *.tbank.ru):
  * INVEST_GRPC_API          (invest-public-api.tbank.ru) — боевой
    контур (реальные деньги).
  * INVEST_GRPC_API_SANDBOX  (sandbox-invest-public-api.tbank.ru) —
    официальная песочница T-Invest (не путать с нашим internal
    paper-режимом MockBroker: здесь ордера реально уходят на сторону
    брокера, просто в изолированном тестовом контуре). ВАЖНО: песочница
    живёт на ОТДЕЛЬНОМ наборе методов SDK (SandboxService.*, не
    UsersService/OperationsService/OrdersService) — счета, портфель,
    заявки в sandbox не пересекаются с боевыми и адресуются иначе.
    Рыночные данные (котировки/стакан/свечи/спецификации инструментов)
    для обоих контуров общие — MarketDataService/InstrumentsService
    используются одинаково независимо от sandbox.

Секреты: токен и account_id читаются ТОЛЬКО из переменных окружения
(имена задаются в config.yaml: broker.token_env, broker.account_id_env;
имена переменных TINVEST_TOKEN/TINVEST_ACCOUNT_ID — наш собственный
выбор для конфига, к названию пакета отношения не имеют, менять не
обязательно). В коде и YAML — только имена переменных, никогда сами
значения.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, TypeVar

from trading_robot.domain.types import (
    AccountState,
    Bar,
    Instrument,
    InstrumentClass,
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

try:
    from t_tech.invest import (
        CandleInterval,
        Client,
        InstrumentIdType,
        OrderDirection,
        OrderExecutionReportStatus,
        OrderType,
        TimeInForceType,
    )
    from t_tech.invest.constants import INVEST_GRPC_API, INVEST_GRPC_API_SANDBOX
    from t_tech.invest.utils import decimal_to_money, decimal_to_quotation, money_to_decimal, quotation_to_decimal

    _TINKOFF_IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # пакет t-tech-investments не установлен
    _TINKOFF_IMPORT_ERROR = exc

logger = logging.getLogger("trading_robot.brokers.tinvest_adapter")


class TInvestConfigError(Exception):
    pass


# Интервал баров нашего домена ("1min", "5min", "1h", "1d", ...) -> имя
# CandleInterval SDK и приблизительная длительность одной свечи (для
# расчёта окна from_/to запроса).
_INTERVAL_MAP: dict[str, tuple[str, timedelta]] = {
    "1min": ("CANDLE_INTERVAL_1_MIN", timedelta(minutes=1)),
    "5min": ("CANDLE_INTERVAL_5_MIN", timedelta(minutes=5)),
    "15min": ("CANDLE_INTERVAL_15_MIN", timedelta(minutes=15)),
    "1h": ("CANDLE_INTERVAL_HOUR", timedelta(hours=1)),
    "1d": ("CANDLE_INTERVAL_DAY", timedelta(days=1)),
}

# T-Invest требует, чтобы order_id в post_order/post_sandbox_order был ЛИБО
# пустым, ЛИБО валидным UUID (наш внутренний client_order_id — читаемая
# строка вида "GRID-<hex>", не UUID, и API реально отклоняет такие значения
# ошибкой "order_id should be empty or uuid" — воспроизведено вживую).
# uuid5 от фиксированного namespace детерминирован: один и тот же
# client_order_id всегда даёт один и тот же UUID, так что идемпотентность
# (повторный такт не дублирует заявку) сохраняется, просто в другом формате.
_ORDER_ID_NAMESPACE = uuid.UUID("d9b1f9a0-6b1a-4f7e-9c1a-9e2f6a2f7c31")


def _order_id_for(client_order_id: str) -> str:
    return str(uuid.uuid5(_ORDER_ID_NAMESPACE, client_order_id))


_T = TypeVar("_T")
# Таймаут на КАЖДЫЙ отдельный вызов gRPC-метода SDK. У SDK нет дефолтного
# таймаута — без него сетевая проблема (заблокированный исходящий gRPC/443,
# зависший TLS-хендшейк и т.п.) вешает такт робота НАВСЕГДА и молча: ни
# ошибки, ни следующей записи в журнал. Обёртка ниже гарантирует, что зависший
# вызов превращается в понятную ошибку максимум через это время.
_CALL_TIMEOUT_SECONDS = 10.0

# T-Invest ограничивает частоту ВЫСТАВЛЕНИЯ ЗАЯВОК ~2 запроса/сек на
# пользователя (видно из ошибки самого API: `ratelimit_limit='2, 2;w=1'` на
# post_order/post_sandbox_order). Это лимит конкретно ordery-эндпоинта, не
# общий на все методы SDK — рыночные данные и портфель у него свои, более
# высокие лимиты. Поэтому троттлим ТОЛЬКО place_limit_order/cancel_order
# (см. _call(..., throttle=True) в местах их вызова), а не вообще каждый
# вызов: более ранняя версия этого фикса вешала паузу глобально на все
# запросы (включая котировки на каждом такте) и из-за этого один такт стал
# занимать ~10-11с вместо настроенных tick_interval_seconds=5с — цены на
# панели выглядели "замершими", хотя робот на самом деле просто медленно
# делал такт.
_ORDER_CALL_MIN_INTERVAL_SECONDS = 0.55


class TInvestAdapter:
    """Реализует Protocol BrokerAdapter (см. interfaces/broker.py) поверх
    официального SDK. Не смешивать с методами других брокеров (ALOR/Finam/
    BCS) — для них пишется отдельный класс с тем же Protocol.
    """

    def __init__(
        self,
        token_env: str = "TINVEST_TOKEN",
        account_id_env: str = "TINVEST_ACCOUNT_ID",
        sandbox: bool = True,
        call_timeout_seconds: float = _CALL_TIMEOUT_SECONDS,
    ) -> None:
        self._token_env = token_env
        self._account_id_env = account_id_env
        self._sandbox = sandbox
        self._call_timeout = call_timeout_seconds
        self._client_cm: Any = None
        self._services: Any = None
        self._connected = False
        self._account_id: str | None = None
        # Кэш "наш Instrument -> объект инструмента SDK (Share/Bond/Future)",
        # и обратный кэш figi -> Instrument для сборки позиций из портфеля.
        self._instrument_cache: dict[str, Any] = {}
        self._figi_to_instrument: dict[str, Instrument] = {}
        # SDK не отдаёт наш client_order_id обратно в get_orders() (только в
        # ответе post_order, в момент выставления) — восстанавливаем его для
        # уже висящих заявок через broker_order_id, который совпадает с тем
        # order_id, что мы сами передали при выставлении (см. place_limit_order
        # и _order_id_for). Тот же per-process-кэш паттерн и то же ограничение,
        # что у _figi_to_instrument: после рестарта процесса пуст для заявок,
        # выставленных до рестарта (сама идемпотентность заявок от этого не
        # страдает — она держится на known_client_order_ids в state.json).
        self._order_id_to_client_id: dict[str, str] = {}
        # Один воркер: gRPC-вызовы этого адаптера в рамках одного такта и так
        # выполняются последовательно (см. engine/loop.py — синхронный цикл),
        # пул нужен только чтобы дать вызову таймаут через Future.result().
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="tinvest-io")
        self._last_order_call_at: float = 0.0

    def _call(self, fn: Callable[..., _T], *args: Any, throttle: bool = False, **kwargs: Any) -> _T:
        if throttle:
            elapsed = time.monotonic() - self._last_order_call_at
            if elapsed < _ORDER_CALL_MIN_INTERVAL_SECONDS:
                time.sleep(_ORDER_CALL_MIN_INTERVAL_SECONDS - elapsed)
            self._last_order_call_at = time.monotonic()
        future = self._executor.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=self._call_timeout)
        except concurrent.futures.TimeoutError as exc:
            name = getattr(fn, "__qualname__", getattr(fn, "__name__", repr(fn)))
            raise TInvestConfigError(
                f"T-Invest API не ответил за {self._call_timeout:.0f}с на вызове {name} — "
                "проверьте исходящий доступ с сервера к "
                f"{'sandbox-' if self._sandbox else ''}invest-public-api.tinkoff.ru:443 "
                "(firewall/security group, DNS)"
            ) from exc

    def _read_env(self, name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise TInvestConfigError(f"переменная окружения {name} не задана")
        return value

    def _require_sdk(self) -> None:
        if _TINKOFF_IMPORT_ERROR is not None:
            raise TInvestConfigError(
                "пакет t-tech-investments не установлен. Установите: "
                "pip install t-tech-investments --index-url "
                "https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple"
            ) from _TINKOFF_IMPORT_ERROR

    # -- connection ---------------------------------------------------------
    def connect(self) -> None:
        self._require_sdk()
        token = self._read_env(self._token_env)
        target = INVEST_GRPC_API_SANDBOX if self._sandbox else INVEST_GRPC_API
        self._client_cm = Client(token, target=target)
        self._services = self._call(self._client_cm.__enter__)
        self._connected = True
        self._account_id = self._resolve_account_id()

    def disconnect(self) -> None:
        if self._client_cm is not None:
            self._client_cm.__exit__(None, None, None)
        self._connected = False
        self._client_cm = None
        self._services = None
        self._executor.shutdown(wait=False)

    def is_connected(self) -> bool:
        return self._connected

    def _require_connected(self) -> None:
        if not self._connected or self._services is None:
            raise TInvestConfigError("TInvestAdapter не подключён — вызовите connect() перед использованием")

    def _resolve_account_id(self) -> str:
        # Sandbox и боевой контур адресуют счета РАЗНЫМИ методами:
        # SandboxService.get_sandbox_accounts() в песочнице,
        # UsersService.get_accounts() на боевом контуре.
        if self._sandbox:
            accounts = self._call(self._services.sandbox.get_sandbox_accounts).accounts
        else:
            accounts = self._call(self._services.users.get_accounts).accounts

        configured = os.environ.get(self._account_id_env)
        if configured:
            for acc in accounts:
                if acc.id == configured:
                    return acc.id
            raise TInvestConfigError(
                f"счёт {configured} (из {self._account_id_env}) не найден среди счетов пользователя"
            )

        if not accounts:
            if self._sandbox:
                # Sandbox — изолированная песочница без реальных денег: в
                # отличие от боевого контура здесь безопасно создать счёт
                # автоматически, если пользователь явно не указал
                # TINVEST_ACCOUNT_ID. Заводим виртуальный баланс сразу,
                # иначе счёт будет с нулевым капиталом и робот не сможет
                # выставить ни одной заявки (cash_reserve заблокирует вход).
                return self._create_sandbox_account()
            raise TInvestConfigError(
                "нет ни одного счёта у пользователя — откройте счёт в приложении/личном кабинете T-Инвестиций"
            )
        return accounts[0].id

    def _create_sandbox_account(self) -> str:
        response = self._call(self._services.sandbox.open_sandbox_account, name="trading-robot")
        account_id = response.account_id
        top_up = Decimal(os.environ.get("TINVEST_SANDBOX_INITIAL_CASH", "1000000"))
        self._call(
            self._services.sandbox.sandbox_pay_in,
            account_id=account_id,
            amount=decimal_to_money(top_up, "rub"),
        )
        logger.info(
            "sandbox: создан новый счёт %s и зачислено %s RUB виртуальных денег "
            "(переопределяется переменной окружения TINVEST_SANDBOX_INITIAL_CASH)",
            account_id, top_up,
        )
        return account_id

    # -- instrument resolution ------------------------------------------------
    def _resolve_instrument(self, instrument: Instrument):
        cached = self._instrument_cache.get(instrument.key)
        if cached is not None:
            return cached

        # instrument.board используется как class_code (для TQBR акций это
        # совпадает напрямую; для FORTS/срочного рынка сверьте актуальный
        # class_code конкретного контракта в личном кабинете/API — он не
        # всегда буквально равен "FORTS").
        id_type = InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER
        if instrument.instrument_class == InstrumentClass.SHARE:
            resp = self._call(
                self._services.instruments.share_by, id_type=id_type, class_code=instrument.board, id=instrument.ticker
            )
        elif instrument.instrument_class == InstrumentClass.BOND:
            resp = self._call(
                self._services.instruments.bond_by, id_type=id_type, class_code=instrument.board, id=instrument.ticker
            )
        elif instrument.instrument_class == InstrumentClass.FUTURE:
            resp = self._call(
                self._services.instruments.future_by, id_type=id_type, class_code=instrument.board, id=instrument.ticker
            )
        else:
            raise TInvestConfigError(f"неподдерживаемый instrument_class: {instrument.instrument_class}")

        obj = resp.instrument
        self._instrument_cache[instrument.key] = obj
        self._figi_to_instrument[obj.figi] = instrument
        return obj

    def _resolve_by_figi(self, figi: str) -> Instrument:
        """Обратное разрешение figi -> наш Instrument для позиций/заявок,
        которые брокер вернул по инструменту, ещё не встречавшемуся в этом
        запуске процесса (свежий рестарт сервиса, инструмент вне текущего
        config.instruments/выбора на панели и т.п.). Без этого get_account()
        молча ТЕРЯЛ такие позиции (see: InstrumentsService.get_instrument_by
        отдаёт generic Instrument с теми же lot/min_price_increment/currency,
        что и Share/Bond/Future — совместим с тем, что использует
        get_instrument_spec).
        """
        cached = self._figi_to_instrument.get(figi)
        if cached is not None:
            return cached
        resp = self._call(
            self._services.instruments.get_instrument_by,
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=figi,
        )
        raw = resp.instrument
        type_map = {
            "share": InstrumentClass.SHARE,
            "bond": InstrumentClass.BOND,
            "future": InstrumentClass.FUTURE,
        }
        if raw.instrument_type not in type_map:
            # T-Invest возвращает в portfolio.positions/get_orders НЕ только
            # реальные бумаги, но и служебные записи — например валютный
            # остаток (рубли) как псевдо-инструмент с тикером вида
            # "RUB000UTSTOM", instrument_type="currency". Это те же деньги,
            # что уже учтены в account.cash — трактовать как акцию/облигацию
            # по умолчанию (было раньше) НЕЛЬЗЯ: котировок для него нет,
            # mark_price уходит в 0, и P&L искажается на всю сумму остатка.
            raise TInvestConfigError(
                f"figi={figi}: instrument_type={raw.instrument_type!r} не поддерживается "
                "(не share/bond/future — вероятно валютная или иная служебная запись)"
            )
        instrument = Instrument(ticker=raw.ticker, board=raw.class_code, instrument_class=type_map[raw.instrument_type])
        self._instrument_cache[instrument.key] = raw
        self._figi_to_instrument[figi] = instrument
        return instrument

    # -- market data ----------------------------------------------------------
    def get_quote(self, instrument: Instrument) -> Quote:
        self._require_connected()
        obj = self._resolve_instrument(instrument)
        last_resp = self._call(self._services.market_data.get_last_prices, figi=[obj.figi])
        last = quotation_to_decimal(last_resp.last_prices[0].price) if last_resp.last_prices else Decimal("0")

        ob = self._call(self._services.market_data.get_order_book, figi=obj.figi, depth=1)
        bid = quotation_to_decimal(ob.bids[0].price) if ob.bids else last
        ask = quotation_to_decimal(ob.asks[0].price) if ob.asks else last
        timestamp = ob.orderbook_ts or datetime.now(timezone.utc)

        return Quote(instrument=instrument, bid=bid, ask=ask, last=last, timestamp=timestamp)

    def get_orderbook(self, instrument: Instrument, depth: int) -> OrderBook:
        self._require_connected()
        obj = self._resolve_instrument(instrument)
        ob = self._call(self._services.market_data.get_order_book, figi=obj.figi, depth=depth)
        bids = tuple(OrderBookLevel(price=quotation_to_decimal(o.price), lots=o.quantity) for o in ob.bids)
        asks = tuple(OrderBookLevel(price=quotation_to_decimal(o.price), lots=o.quantity) for o in ob.asks)
        timestamp = ob.orderbook_ts or datetime.now(timezone.utc)
        return OrderBook(instrument=instrument, bids=bids, asks=asks, timestamp=timestamp)

    def get_bars(self, instrument: Instrument, interval: str, limit: int) -> list[Bar]:
        self._require_connected()
        if interval not in _INTERVAL_MAP:
            raise TInvestConfigError(f"неподдерживаемый interval={interval!r}, известны: {list(_INTERVAL_MAP)}")
        interval_name, bar_duration = _INTERVAL_MAP[interval]
        candle_interval = getattr(CandleInterval, interval_name)

        obj = self._resolve_instrument(instrument)
        now = datetime.now(timezone.utc)
        # Небольшой запас (x2) на случай пропусков свечей (выходные/паузы в торгах).
        from_ = now - bar_duration * limit * 2

        resp = self._call(
            self._services.market_data.get_candles,
            figi=obj.figi, from_=from_, to=now, interval=candle_interval, limit=limit,
        )
        candles = resp.candles[-limit:]
        return [
            Bar(
                instrument=instrument,
                open=quotation_to_decimal(c.open),
                high=quotation_to_decimal(c.high),
                low=quotation_to_decimal(c.low),
                close=quotation_to_decimal(c.close),
                volume=c.volume,
                timestamp=c.time,
            )
            for c in candles
        ]

    def get_instrument_spec(self, instrument: Instrument) -> InstrumentSpec:
        self._require_connected()
        obj = self._resolve_instrument(instrument)
        price_step = quotation_to_decimal(obj.min_price_increment)

        initial_margin_per_lot = None
        if instrument.instrument_class == InstrumentClass.FUTURE and hasattr(obj, "initial_margin_on_buy"):
            # ГО на покупку/продажу может отличаться — берём консервативно
            # большее из двух, чтобы риск-проверка не занижала требование.
            # hasattr: если obj пришёл из _resolve_by_figi (общий Instrument,
            # не Future) — полей ГО там нет, initial_margin_per_lot=None
            # (проверка ГО просто пропускается выше по стеку, см. risk_manager).
            buy_margin = money_to_decimal(obj.initial_margin_on_buy)
            sell_margin = money_to_decimal(obj.initial_margin_on_sell)
            initial_margin_per_lot = max(buy_margin, sell_margin)

        return InstrumentSpec(
            instrument=instrument,
            lot_size=obj.lot,
            price_step=price_step,
            currency=obj.currency,
            initial_margin_per_lot=initial_margin_per_lot,
        )

    # -- account ----------------------------------------------------------
    def get_account(self) -> AccountState:
        self._require_connected()
        assert self._account_id is not None

        if self._sandbox:
            portfolio = self._call(self._services.sandbox.get_sandbox_portfolio, account_id=self._account_id)
            positions_resp = self._call(self._services.sandbox.get_sandbox_positions, account_id=self._account_id)
            orders_resp = self._call(self._services.sandbox.get_sandbox_orders, account_id=self._account_id)
        else:
            portfolio = self._call(self._services.operations.get_portfolio, account_id=self._account_id)
            positions_resp = self._call(self._services.operations.get_positions, account_id=self._account_id)
            orders_resp = self._call(self._services.orders.get_orders, account_id=self._account_id)

        # PositionsResponse.money — СВОБОДНЫЕ деньги (не зарезервированные под
        # открытые заявки); PositionsResponse.blocked — деньги, заблокированные
        # под уже выставленные (но ещё не исполненные) лимитные заявки. Это НЕ
        # убыток — деньги никуда не делись, просто временно недоступны для
        # НОВЫХ заявок. Если считать equity только по money (как было раньше),
        # каждый новый такт с несколькими открытыми заявками по нескольким
        # инструментам искусственно занижает equity на сумму резерва — именно
        # так дневной стоп-лосс срабатывал ложно сразу после массового
        # выставления сетки по 8 тикерам, при нулевом количестве реальных
        # позиций. AccountState.cash здесь — ПОЛНАЯ сумма (money + blocked):
        # для equity/дневного P&L это правильно; для риск-сайзинга новых
        # заявок это чуть оптимистичнее реальной "свободной" суммы, но это
        # приемлемый компромисс против ложных остановок робота.
        currency = None
        free_cash = Decimal("0")
        blocked_cash = Decimal("0")
        for money in positions_resp.money:
            if currency is None or money.currency.lower() in ("rub", "rur"):
                free_cash = money_to_decimal(money)
                currency = money.currency
        for money in positions_resp.blocked:
            if money.currency.lower() == (currency or "rub").lower():
                blocked_cash += money_to_decimal(money)
        cash = free_cash + blocked_cash
        if currency is None:
            currency = "rub"

        positions: list[Position] = []
        for pos in portfolio.positions:
            instrument = self._figi_to_instrument.get(pos.figi)
            if instrument is None:
                # Позиция по инструменту, который ещё не резолвился в ЭТОМ
                # запуске процесса (свежий рестарт сервиса, инструмент вне
                # текущего выбора на панели и т.п.) — раньше такая позиция
                # молча пропадала из AccountState, что занижало/искажало
                # equity и могло ложно бить по дневному стоп-лоссу.
                # Резолвим по figi напрямую через API, а не пропускаем.
                try:
                    instrument = self._resolve_by_figi(pos.figi)
                except TInvestConfigError:
                    # Штатный случай — валютный остаток или другая служебная
                    # запись в portfolio.positions (см. _resolve_by_figi),
                    # не наша "торгуемая позиция". Тихо пропускаем, без warning
                    # (иначе он будет валить лог на каждом такте).
                    continue
                except Exception:
                    logger.warning(
                        "не удалось резолвить figi=%s для открытой позиции — "
                        "она не попадёт в AccountState/equity, проверьте вручную",
                        pos.figi,
                    )
                    continue
            positions.append(
                Position(
                    instrument=instrument,
                    lots=int(quotation_to_decimal(pos.quantity_lots)),
                    average_price=money_to_decimal(pos.average_position_price),
                )
            )

        orders: list[Order] = []
        for state in orders_resp.orders:
            # ПРИМЕЧАНИЕ: точное наличие поля `figi` в OrderState не подтверждено
            # по проверенному подмножеству полей (order_id/execution_report_status/
            # lots_*/prices/direction/order_type/order_date/currency/stages) — используем
            # getattr с безопасным дефолтом: если поля нет, инструмент не находится
            # в кэше, и заявка просто пропускается (не роняет sync_state). Перед
            # боевым использованием сверьте реальный ответ get_orders() и, если
            # понадобится, замените на instrument_uid/positional_uid — что бы SDK
            # реально ни возвращал.
            state_figi = getattr(state, "figi", "")
            instrument = self._figi_to_instrument.get(state_figi)
            if instrument is None and state_figi:
                try:
                    instrument = self._resolve_by_figi(state_figi)
                except TInvestConfigError:
                    pass  # см. комментарий в get_account() про валютные записи
                except Exception:
                    logger.warning("не удалось резолвить figi=%s для открытой заявки, пропускаю", state_figi)
            if instrument is None:
                continue  # нет figi в ответе SDK или не удалось резолвить
            orders.append(
                Order(
                    # Восстанавливаем через _order_id_to_client_id (см. __init__) —
                    # пусто, только если заявка выставлена до рестарта процесса.
                    client_order_id=self._order_id_to_client_id.get(state.order_id, ""),
                    broker_order_id=state.order_id,
                    instrument=instrument,
                    side=Side.BUY if state.direction == OrderDirection.ORDER_DIRECTION_BUY else Side.SELL,
                    lots=state.lots_requested,
                    # ВАЖНО: initial_order_price — это сумма ВСЕЙ заявки целиком
                    # (price * lot_size * lots), а НЕ цена за одну бумагу — легко
                    # перепутать по названию поля. Цена за штуку — отдельное поле
                    # initial_security_price (проверено живым импортом SDK,
                    # dataclasses.fields(OrderState)). Спутать их — значит
                    # показать/использовать цену, завышенную в lot_size*lots раз.
                    price=money_to_decimal(state.initial_security_price),
                    time_in_force=TimeInForce.DAY,
                    status=_EXEC_STATUS_MAP.get(state.execution_report_status, OrderStatus.ACCEPTED),
                    filled_lots=state.lots_executed,
                )
            )

        used_margin = None  # TODO: UsersService.get_margin_attributes(account_id=...) для ГО по FORTS

        return AccountState(
            cash=cash,
            currency=currency,
            positions=tuple(positions),
            orders=tuple(orders),
            used_margin=used_margin,
            timestamp=datetime.now(timezone.utc),
        )

    def sync_state(self) -> AccountState:
        return self.get_account()

    # -- orders -------------------------------------------------------------
    def place_limit_order(self, request: LimitOrderRequest) -> OrderAck:
        self._require_connected()
        assert self._account_id is not None
        obj = self._resolve_instrument(request.instrument)

        direction = OrderDirection.ORDER_DIRECTION_BUY if request.side == Side.BUY else OrderDirection.ORDER_DIRECTION_SELL
        post_fn = self._services.sandbox.post_sandbox_order if self._sandbox else self._services.orders.post_order
        response = self._call(
            post_fn,
            throttle=True,
            figi=obj.figi,
            quantity=request.lots,
            price=decimal_to_quotation(request.price),
            direction=direction,
            account_id=self._account_id,
            order_type=OrderType.ORDER_TYPE_LIMIT,
            order_id=_order_id_for(request.client_order_id),
            time_in_force=_TIF_MAP.get(request.time_in_force, TimeInForceType.TIME_IN_FORCE_DAY),
        )
        self._order_id_to_client_id[response.order_id] = request.client_order_id
        return OrderAck(
            client_order_id=request.client_order_id,
            broker_order_id=response.order_id,
            status=_EXEC_STATUS_MAP.get(response.execution_report_status, OrderStatus.ACCEPTED),
            message=response.message or "",
        )

    def cancel_order(self, broker_order_id: str) -> None:
        self._require_connected()
        assert self._account_id is not None
        cancel_fn = self._services.sandbox.cancel_sandbox_order if self._sandbox else self._services.orders.cancel_order
        self._call(cancel_fn, throttle=True, account_id=self._account_id, order_id=broker_order_id)


if _TINKOFF_IMPORT_ERROR is None:
    _EXEC_STATUS_MAP = {
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_UNSPECIFIED: OrderStatus.ACCEPTED,
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW: OrderStatus.NEW,
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL: OrderStatus.FILLED,
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_PARTIALLYFILL: OrderStatus.PARTIALLY_FILLED,
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_REJECTED: OrderStatus.REJECTED,
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_CANCELLED: OrderStatus.CANCELLED,
    }
    _TIF_MAP = {
        # У T-Invest нет настоящего GTC для обычных лимитных заявок — заявка
        # живёт в рамках торгового дня. GTC-подобное поведение достигается
        # тем, что робот пере-выставляет уровни сетки на каждый новый
        # торговый день (client_order_id завязан на дату, см.
        # strategy/grid_strategy.py::_stable_client_order_id).
        TimeInForce.DAY: TimeInForceType.TIME_IN_FORCE_DAY,
        TimeInForce.GTC: TimeInForceType.TIME_IN_FORCE_DAY,
        TimeInForce.IOC: TimeInForceType.TIME_IN_FORCE_FILL_AND_KILL,
    }
