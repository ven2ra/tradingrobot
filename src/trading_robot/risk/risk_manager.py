"""Risk — жёсткий слой. Стратегия не может его обойти.

Порядок фильтров всего пайплайна (реализуется в engine/loop.py):
  соединение и Risk -> Regime и ContextFilter -> Strategy -> расчёт объёма -> Execution

Этот модуль отвечает за первую и последнюю проверку: можно ли вообще
рассматривать новые входы сейчас (по времени/соединению/дневному лимиту/
резерву кэша/числу инструментов/весу инструмента), и отдельно —
достаточно ли ГО для конкретной заявки на FORTS.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading_robot.domain.types import AccountState, Instrument, InstrumentClass

MSK = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_daily_loss_pct: Decimal  # DEFAULT 1%
    max_instrument_weight: Decimal  # DEFAULT 20%
    cash_reserve: Decimal
    max_instruments: int  # DEFAULT 3
    entry_start: time  # DEFAULT 10:05 MSK
    entry_cutoff_minutes_before_close: int  # DEFAULT 15
    session_close: time
    emergency_market_flatten: bool = False


@dataclass(frozen=True, slots=True)
class RiskDecision:
    entries_allowed: bool
    reason: str


class DailyPnlTracker:
    """Отслеживает P&L за торговый день относительно стартового капитала дня."""

    def __init__(self, day_start_equity: Decimal) -> None:
        self._day_start_equity = day_start_equity
        self._stopped_out = False

    def update(self, current_equity: Decimal, max_daily_loss_pct: Decimal) -> bool:
        """Возвращает True, если дневной лимит убытка исчерпан (стоп на день)."""
        if self._day_start_equity <= 0:
            return self._stopped_out
        loss_pct = (self._day_start_equity - current_equity) / self._day_start_equity
        if loss_pct >= max_daily_loss_pct:
            self._stopped_out = True
        return self._stopped_out

    @property
    def stopped_out(self) -> bool:
        return self._stopped_out

    def reset_for_new_day(self, day_start_equity: Decimal) -> None:
        self._day_start_equity = day_start_equity
        self._stopped_out = False


def equity_of(account: AccountState, mark_prices: dict[str, Decimal]) -> Decimal:
    """Equity = деньги + рыночная стоимость позиций по последним ценам mark_prices[instrument.key]."""
    total = account.cash
    for pos in account.positions:
        price = mark_prices.get(pos.instrument.key, pos.average_price)
        total += price * pos.lots
    return total


def check_entries_allowed(
    *,
    now_msk: datetime,
    is_connected: bool,
    daily_pnl_tracker: DailyPnlTracker,
    account: AccountState,
    open_instrument_count: int,
    limits: RiskLimits,
) -> RiskDecision:
    """Первый барьер пайплайна: можно ли вообще рассматривать новые входы сейчас."""
    if not is_connected:
        return RiskDecision(False, "no broker connection: new entries forbidden")

    if daily_pnl_tracker.stopped_out:
        return RiskDecision(False, "daily loss limit reached: only flatten/wait until next session")

    local_time = now_msk.astimezone(MSK).time()
    if local_time < limits.entry_start:
        return RiskDecision(False, f"before entry_start {limits.entry_start}")

    cutoff = _minutes_before(limits.session_close, limits.entry_cutoff_minutes_before_close)
    if local_time >= cutoff:
        return RiskDecision(False, f"within {limits.entry_cutoff_minutes_before_close}min of session close")

    if account.cash <= limits.cash_reserve:
        return RiskDecision(False, "cash reserve breached: new entries forbidden")

    if open_instrument_count >= limits.max_instruments:
        return RiskDecision(False, f"max_instruments={limits.max_instruments} reached")

    return RiskDecision(True, "entries allowed")


def _minutes_before(t: time, minutes: int) -> time:
    total_minutes = t.hour * 60 + t.minute - minutes
    total_minutes %= 24 * 60
    return time(hour=total_minutes // 60, minute=total_minutes % 60)


def max_allowed_notional_for_instrument(
    *,
    account: AccountState,
    mark_prices: dict[str, Decimal],
    instrument: Instrument,
    limits: RiskLimits,
) -> Decimal:
    """Сколько ещё можно вложить в инструмент, не нарушив cash_reserve и max_instrument_weight."""
    equity = equity_of(account, mark_prices)
    max_by_weight = equity * limits.max_instrument_weight
    current_key = instrument.key
    current_position_value = Decimal("0")
    for pos in account.positions:
        if pos.instrument.key == current_key:
            price = mark_prices.get(current_key, pos.average_price)
            current_position_value = price * pos.lots
            break

    remaining_by_weight = max_by_weight - current_position_value
    available_cash = account.cash - limits.cash_reserve

    allowed = min(remaining_by_weight, available_cash)
    return max(Decimal("0"), allowed)


def check_forts_margin(
    *,
    account: AccountState,
    instrument: Instrument,
    additional_margin_required: Decimal,
) -> RiskDecision:
    """Проверка достаточности ГО для срочного рынка. Вызывается только для FORTS
    и только если брокер отдаёт used_margin (иначе проверка пропускается выше по стеку).
    """
    if instrument.instrument_class != InstrumentClass.FUTURE:
        return RiskDecision(True, "not a future, margin check n/a")
    if account.used_margin is None:
        return RiskDecision(True, "broker does not report margin, skipping check")

    free_funds = account.cash - account.used_margin
    if free_funds < additional_margin_required:
        return RiskDecision(False, "insufficient free margin (ГО) for FORTS order")
    return RiskDecision(True, "margin sufficient")
