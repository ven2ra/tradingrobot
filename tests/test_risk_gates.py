from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading_robot.domain.types import AccountState
from trading_robot.risk.risk_manager import DailyPnlTracker, RiskLimits, check_entries_allowed

MSK = ZoneInfo("Europe/Moscow")


def _limits(**overrides) -> RiskLimits:
    base = dict(
        max_daily_loss_pct=Decimal("0.01"),
        max_instrument_weight=Decimal("0.20"),
        cash_reserve=Decimal("10000"),
        max_instruments=3,
        entry_start=time(10, 5),
        entry_cutoff_minutes_before_close=15,
        session_close=time(18, 45),
    )
    base.update(overrides)
    return RiskLimits(**base)


def _account(cash: Decimal) -> AccountState:
    return AccountState(cash=cash, currency="RUB", positions=(), orders=())


def _mid_session_time() -> datetime:
    return datetime(2026, 9, 4, 12, 0, tzinfo=MSK)


def test_cash_reserve_blocks_entry():
    limits = _limits(cash_reserve=Decimal("10000"))
    account = _account(cash=Decimal("9000"))  # ниже резерва
    tracker = DailyPnlTracker(day_start_equity=Decimal("9000"))

    decision = check_entries_allowed(
        now_msk=_mid_session_time(),
        is_connected=True,
        daily_pnl_tracker=tracker,
        account=account,
        open_instrument_count=0,
        limits=limits,
    )
    assert decision.entries_allowed is False
    assert "cash reserve" in decision.reason


def test_cash_above_reserve_allows_entry():
    limits = _limits(cash_reserve=Decimal("10000"))
    account = _account(cash=Decimal("50000"))
    tracker = DailyPnlTracker(day_start_equity=Decimal("50000"))

    decision = check_entries_allowed(
        now_msk=_mid_session_time(),
        is_connected=True,
        daily_pnl_tracker=tracker,
        account=account,
        open_instrument_count=0,
        limits=limits,
    )
    assert decision.entries_allowed is True


def test_max_instruments_blocks_entry():
    limits = _limits(max_instruments=3)
    account = _account(cash=Decimal("500000"))
    tracker = DailyPnlTracker(day_start_equity=Decimal("500000"))

    decision = check_entries_allowed(
        now_msk=_mid_session_time(),
        is_connected=True,
        daily_pnl_tracker=tracker,
        account=account,
        open_instrument_count=3,  # уже на лимите
        limits=limits,
    )
    assert decision.entries_allowed is False
    assert "max_instruments" in decision.reason


def test_connection_loss_blocks_entry():
    limits = _limits()
    account = _account(cash=Decimal("500000"))
    tracker = DailyPnlTracker(day_start_equity=Decimal("500000"))

    decision = check_entries_allowed(
        now_msk=_mid_session_time(),
        is_connected=False,
        daily_pnl_tracker=tracker,
        account=account,
        open_instrument_count=0,
        limits=limits,
    )
    assert decision.entries_allowed is False
    assert "connection" in decision.reason


def test_daily_stop_loss_blocks_further_entries_until_flatten_only():
    limits = _limits(max_daily_loss_pct=Decimal("0.01"))
    day_start_equity = Decimal("100000")
    tracker = DailyPnlTracker(day_start_equity=day_start_equity)

    # Убыток 2% > лимита 1% -> стоп на день.
    stopped = tracker.update(current_equity=Decimal("98000"), max_daily_loss_pct=limits.max_daily_loss_pct)
    assert stopped is True
    assert tracker.stopped_out is True

    account = _account(cash=Decimal("500000"))
    decision = check_entries_allowed(
        now_msk=_mid_session_time(),
        is_connected=True,
        daily_pnl_tracker=tracker,
        account=account,
        open_instrument_count=0,
        limits=limits,
    )
    assert decision.entries_allowed is False
    assert "daily loss" in decision.reason


def test_entry_before_entry_start_blocked():
    limits = _limits(entry_start=time(10, 5))
    account = _account(cash=Decimal("500000"))
    tracker = DailyPnlTracker(day_start_equity=Decimal("500000"))
    early = datetime(2026, 9, 4, 9, 30, tzinfo=MSK)

    decision = check_entries_allowed(
        now_msk=early,
        is_connected=True,
        daily_pnl_tracker=tracker,
        account=account,
        open_instrument_count=0,
        limits=limits,
    )
    assert decision.entries_allowed is False


def test_entry_near_close_blocked():
    limits = _limits(session_close=time(18, 45), entry_cutoff_minutes_before_close=15)
    account = _account(cash=Decimal("500000"))
    tracker = DailyPnlTracker(day_start_equity=Decimal("500000"))
    near_close = datetime(2026, 9, 4, 18, 35, tzinfo=MSK)

    decision = check_entries_allowed(
        now_msk=near_close,
        is_connected=True,
        daily_pnl_tracker=tracker,
        account=account,
        open_instrument_count=0,
        limits=limits,
    )
    assert decision.entries_allowed is False
