"""Главный цикл робота.

Синхронный (не asyncio) цикл: один брокерский коннект, инструменты
обрабатываются последовательно на каждом такте, частота тактов низкая
(секунды), узкое место — сетевой I/O одного клиента, а не CPU/конкурентность.
asyncio добавил бы сложность (пришлось бы либо гнать async-версии
BrokerAdapter, либо заворачивать sync SDK в executor) без выигрыша при
такой частоте — поэтому простой sync-цикл с понятным порядком фильтров.

Порядок строго: соединение и Risk -> Regime и ContextFilter -> Strategy ->
расчёт объёма -> Execution.
"""
from __future__ import annotations

import logging
import time as time_module
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_robot.config.loader import RootConfig
from trading_robot.context.context_filter import CalendarContextFilter, ContextVerdict, combine_verdicts
from trading_robot.domain.types import AccountState, Instrument, InstrumentClass, InstrumentSpec, OrderStatus, Side
from trading_robot.features.features import compute_features
from trading_robot.interfaces.broker import BrokerAdapter
from trading_robot.journal.account_snapshot import build_snapshot, write_account_snapshot
from trading_robot.journal.journal import Journal
from trading_robot.regime.regime import Regime, RegimeThresholds, classify_regime, entries_allowed
from trading_robot.risk.risk_manager import (
    DailyPnlTracker,
    RiskLimits,
    check_entries_allowed,
    check_forts_margin,
    equity_of,
    max_allowed_notional_for_instrument,
)
from trading_robot.store.instrument_selection import InstrumentSelectionStore, SelectedInstrument
from trading_robot.store.state_store import RobotState, StateStore
from trading_robot.strategy.grid_strategy import (
    GridConfig,
    build_grid_plan,
    compute_lots_for_level,
    level_to_order_request,
)

MSK = ZoneInfo("Europe/Moscow")

logger = logging.getLogger("trading_robot.engine")


def instruments_from_config(cfg: RootConfig) -> list[Instrument]:
    result = []
    for ic in cfg.instruments:
        expiration = datetime.fromisoformat(ic.expiration) if ic.expiration else None
        result.append(
            Instrument(
                ticker=ic.ticker,
                board=ic.board,
                instrument_class=InstrumentClass(ic.instrument_class),
                expiration=expiration,
            )
        )
    return result


def instrument_from_selected(sel: SelectedInstrument) -> Instrument:
    return Instrument(ticker=sel.ticker, board=sel.board, instrument_class=InstrumentClass(sel.instrument_class))


def risk_limits_from_config(cfg: RootConfig) -> RiskLimits:
    return RiskLimits(
        max_daily_loss_pct=cfg.risk.max_daily_loss_pct,
        max_instrument_weight=cfg.risk.max_instrument_weight,
        cash_reserve=cfg.risk.cash_reserve,
        max_instruments=cfg.risk.max_instruments,
        entry_start=cfg.risk.entry_start,
        entry_cutoff_minutes_before_close=cfg.risk.entry_cutoff_min,
        session_close=cfg.session.close_time,
        emergency_market_flatten=cfg.risk.emergency_market_flatten,
    )


def regime_thresholds_from_config(cfg: RootConfig) -> RegimeThresholds:
    return RegimeThresholds(
        shock_volatility=cfg.regime.shock_volatility,
        low_liquidity_spread_bps=cfg.regime.low_liquidity_spread_bps,
        low_liquidity_volume_ratio=cfg.regime.low_liquidity_volume_ratio,
        trend_return_threshold=cfg.regime.trend_return_threshold,
        max_age_ms_for_classification=cfg.regime.max_age_ms_for_classification,
    )


def grid_config_from_config(cfg: RootConfig) -> GridConfig:
    return GridConfig(
        max_levels=cfg.strategy.max_levels,
        max_inventory_lots=cfg.strategy.max_inventory_lots,
        step_volatility_multiplier=cfg.strategy.step_volatility_multiplier,
        min_step_price_steps=cfg.strategy.min_step_price_steps,
        level_notional_fraction=cfg.strategy.level_notional_fraction,
    )


class RobotEngine:
    def __init__(
        self,
        cfg: RootConfig,
        broker: BrokerAdapter,
        journal: Journal,
        state_store: StateStore,
        context_filter: CalendarContextFilter | None = None,
        llm_provider=None,  # ExternalVerdictProvider | None, см. context/llm_context.py
    ) -> None:
        self._cfg = cfg
        self._broker = broker
        self._journal = journal
        self._store = state_store
        self._config_instruments = instruments_from_config(cfg)
        self._instrument_selection_store = InstrumentSelectionStore(Path(cfg.selected_instruments_path))
        self._instruments = self._load_instruments()
        self._risk_limits = risk_limits_from_config(cfg)
        self._regime_thresholds = regime_thresholds_from_config(cfg)
        self._grid_config = grid_config_from_config(cfg)
        self._context_filter = context_filter or CalendarContextFilter(events=[])
        self._llm_provider = llm_provider if cfg.llm.enabled else None

        self._state = state_store.load()
        self._daily_tracker: DailyPnlTracker | None = None
        self._account_snapshot_path = Path(cfg.account_snapshot_path)

    def _load_instruments(self) -> list[Instrument]:
        """Список инструментов на этот такт: то, что выбрано через веб-панель
        (data/selected_instruments.json), либо — если панель им ни разу не
        пользовалась — статический список из config.instruments.
        """
        selected = self._instrument_selection_store.load()
        if selected is None:
            return self._config_instruments
        return [instrument_from_selected(s) for s in selected]

    def start(self) -> None:
        self._broker.connect()
        account = self._broker.sync_state()
        self._journal.record(
            ticker="*",
            regime="n/a",
            action="sync",
            reason="startup sync_state: внутреннее состояние приведено к брокеру",
            account=account,
        )
        self._reconcile_day(account)

    def _fetch_marks(self, account: AccountState | None = None) -> tuple[dict[str, Decimal], dict[str, InstrumentSpec]]:
        """mark_prices — цена за единицу инструмента; specs — для домножения на lot_size.

        Котирует не только self._instruments (текущий выбор с панели/конфига),
        но и любые инструменты из открытых позиций account — иначе после
        смены списка акций через веб-панель позиции по СТАРЫМ тикерам
        выпадают из equity_of() (specs.get(key) is None -> пропуск),
        занижая equity и ложно бьют по дневному стоп-лоссу.
        """
        mark_prices: dict[str, Decimal] = {}
        specs: dict[str, InstrumentSpec] = {}
        instruments = list(self._instruments)
        known_keys = {i.key for i in instruments}
        if account is not None:
            for pos in account.positions:
                if pos.instrument.key not in known_keys:
                    known_keys.add(pos.instrument.key)
                    instruments.append(pos.instrument)

        for instrument in instruments:
            try:
                quote = self._broker.get_quote(instrument)
                specs[instrument.key] = self._broker.get_instrument_spec(instrument)
                mark_prices[instrument.key] = (quote.bid + quote.ask) / Decimal("2")
            except Exception:
                logger.warning("failed to fetch quote/spec for %s", instrument.key)
        return mark_prices, specs

    def _reconcile_day(self, account: AccountState) -> None:
        today = datetime.now(MSK).date().isoformat()
        if self._state.trading_day != today:
            mark_prices, specs = self._fetch_marks(account)
            equity = equity_of(account, mark_prices, specs)
            self._state.trading_day = today
            self._state.day_start_equity = str(equity)
            self._state.daily_stopped_out = False
            self._store.save(self._state)
        self._daily_tracker = DailyPnlTracker(Decimal(self._state.day_start_equity or str(account.cash)))
        if self._state.daily_stopped_out:
            self._daily_tracker._stopped_out = True  # восстановление после рестарта в тот же день

    def run_forever(self) -> None:
        self.start()
        while True:
            try:
                self.run_tick()
            except Exception:
                logger.exception("unhandled error in tick")
            time_module.sleep(self._cfg.tick_interval_seconds)

    def run_tick(self) -> None:
        now = datetime.now(MSK)
        self._instruments = self._load_instruments()
        account = self._broker.sync_state()

        mark_prices, specs = self._fetch_marks(account)
        equity = equity_of(account, mark_prices, specs)

        snapshot = build_snapshot(account, mark_prices, specs, equity, now)
        try:
            write_account_snapshot(self._account_snapshot_path, snapshot)
        except OSError:
            logger.exception("failed to write account snapshot for web monitor")

        assert self._daily_tracker is not None
        stopped_out = self._daily_tracker.update(equity, self._risk_limits.max_daily_loss_pct)
        if stopped_out and not self._state.daily_stopped_out:
            self._state.daily_stopped_out = True
            self._store.save(self._state)

        open_instrument_count = len({p.instrument.key for p in account.positions if p.lots != 0})

        risk_decision = check_entries_allowed(
            now_msk=now,
            is_connected=self._broker.is_connected(),
            daily_pnl_tracker=self._daily_tracker,
            account=account,
            open_instrument_count=open_instrument_count,
            limits=self._risk_limits,
        )

        for instrument in self._instruments:
            try:
                self._process_instrument(instrument, account, mark_prices, specs, risk_decision, now)
            except Exception:
                # Сбой по ОДНОМУ инструменту (например брокер отклонил заявку с
                # ошибкой, не входящей в штатные пути ниже) не должен обрывать
                # весь такт — иначе все инструменты ПОСЛЕ проблемного в списке
                # молча перестают обрабатываться на каждом такте без единой
                # записи в журнал, что выглядит как "робот игнорирует бумаги".
                logger.exception("unhandled error processing instrument %s, continuing with the rest", instrument.ticker)
                try:
                    self._journal.record(
                        ticker=instrument.ticker, regime="n/a", action="skip",
                        reason="internal error while processing this instrument (see server log)",
                        account=account,
                    )
                except Exception:
                    logger.exception("failed to even journal the error above for %s", instrument.ticker)

    def _process_instrument(self, instrument, account, mark_prices, specs, risk_decision, now: datetime) -> None:
        ticker = instrument.ticker

        if not risk_decision.entries_allowed:
            self._journal.record(
                ticker=ticker,
                regime="n/a",
                action="skip",
                reason=risk_decision.reason,
                account=account,
            )
            return

        try:
            spec = self._broker.get_instrument_spec(instrument)
            quote = self._broker.get_quote(instrument)
            bars = self._broker.get_bars(instrument, interval="1min", limit=self._cfg.features.mid_window + 1)
        except Exception as exc:
            self._journal.record(
                ticker=ticker, regime="n/a", action="skip",
                reason=f"market data error: {exc}", account=account,
            )
            return

        features = compute_features(
            quote=quote,
            bars=bars,
            short_window=self._cfg.features.short_window,
            mid_window=self._cfg.features.mid_window,
            stale_threshold_ms=self._cfg.features.stale_threshold_ms,
            now=now,
        )
        regime = classify_regime(features, self._regime_thresholds)

        if regime in (Regime.UPTREND, Regime.DOWNTREND):
            self._state.last_trend_by_instrument[instrument.key] = regime.value
            self._store.save(self._state)

        if not entries_allowed(regime):
            self._journal.record(
                ticker=ticker, regime=regime.value, action="skip",
                reason=f"regime {regime.value} forbids new entries", account=account,
            )
            return

        calendar_verdict = self._context_filter.evaluate(instrument, now.date())
        external_verdict: ContextVerdict | None = None
        if self._llm_provider is not None:
            external_verdict = self._llm_provider.get_verdict(instrument, now.date())
        context_verdict = combine_verdicts(calendar_verdict, external_verdict)

        if not context_verdict.trade_allowed:
            self._journal.record(
                ticker=ticker, regime=regime.value, action="skip",
                reason=f"context filter: {context_verdict.reason}", account=account,
            )
            return

        trend_direction = None
        last_trend = self._state.last_trend_by_instrument.get(instrument.key)
        if last_trend in (Regime.UPTREND.value, Regime.DOWNTREND.value):
            trend_direction = Regime(last_trend)

        current_inventory = 0
        for pos in account.positions:
            if pos.instrument.key == instrument.key:
                current_inventory = pos.lots
                break

        tick_bucket = now.date().isoformat()
        plan = build_grid_plan(
            instrument=instrument,
            spec=spec,
            mid_price=features.mid,
            volatility=features.volatility,
            regime=regime,
            current_inventory_lots=current_inventory,
            trend_direction=trend_direction,
            config=self._grid_config,
            tick_bucket=tick_bucket,
        )

        if not plan.levels:
            self._journal.record(
                ticker=ticker, regime=regime.value, action="skip",
                reason=plan.reason, account=account,
            )
            return

        allowed_notional = max_allowed_notional_for_instrument(
            account=account, mark_prices=mark_prices, specs=specs, instrument=instrument, limits=self._risk_limits,
        )
        per_level_notional = allowed_notional * self._grid_config.level_notional_fraction * context_verdict.size_multiplier

        for level in plan.levels:
            if level.client_order_id in self._state.known_client_order_ids:
                continue  # уже отправляли этот логический уровень в этом торговом дне

            lots = compute_lots_for_level(allowed_notional=per_level_notional, price=level.price, spec=spec)
            if lots <= 0:
                self._journal.record(
                    ticker=ticker, regime=regime.value, action="skip",
                    reason="computed lots=0 after risk/notional constraints",
                    account=account, price=level.price,
                )
                continue

            if instrument.instrument_class == InstrumentClass.FUTURE and spec.initial_margin_per_lot is not None:
                margin_decision = check_forts_margin(
                    account=account, instrument=instrument,
                    additional_margin_required=spec.initial_margin_per_lot * lots,
                )
                if not margin_decision.entries_allowed:
                    self._journal.record(
                        ticker=ticker, regime=regime.value, action="skip",
                        reason=margin_decision.reason, account=account,
                    )
                    continue

            request = level_to_order_request(level, instrument, lots)
            if request is None:
                continue

            try:
                ack = self._broker.place_limit_order(request)
            except Exception as exc:
                # Брокер реально отклоняет/не может исполнить отдельные заявки
                # (неверная цена/лот для конкретной бумаги, недостаточно ГО,
                # временный сбой сети и т.п.) — это не должно ронять
                # обработку остальных уровней/инструментов, только эту заявку.
                # client_order_id НЕ добавляем в known_client_order_ids —
                # заявка реально не ушла, попробуем снова на следующем такте.
                self._journal.record(
                    ticker=ticker, regime=regime.value, action="skip",
                    reason=f"place_limit_order failed: {exc}", account=account,
                    client_order_id=level.client_order_id, price=level.price, lots=lots,
                )
                continue
            self._state.known_client_order_ids.append(level.client_order_id)
            self._store.save(self._state)

            self._journal.record(
                ticker=ticker, regime=regime.value, action="enter",
                reason=f"grid level {level.level_index} {level.side.value}",
                account=account, client_order_id=ack.client_order_id,
                broker_order_id=ack.broker_order_id, price=level.price, lots=lots,
                status=ack.status.value,
            )
