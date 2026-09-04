from __future__ import annotations

from datetime import date
from decimal import Decimal

from trading_robot.context.context_filter import (
    CalendarContextFilter,
    CalendarEvent,
    ContextVerdict,
    combine_verdicts,
)


def test_calendar_blackout_blocks_trade(instrument):
    events = [
        CalendarEvent(
            instrument_ticker=instrument.ticker,
            event_date=date(2026, 9, 4),
            kind="dividend",
            blackout_days_before=1,
            blackout_days_after=0,
        )
    ]
    cf = CalendarContextFilter(events=events)
    verdict = cf.evaluate(instrument, as_of=date(2026, 9, 4))
    assert verdict.trade_allowed is False


def test_external_llm_false_blocks_even_if_calendar_ok(instrument):
    base = ContextVerdict(trade_allowed=True, size_multiplier=Decimal("1"), reason="no events")
    external = ContextVerdict(trade_allowed=False, size_multiplier=Decimal("0"), reason="llm veto")

    combined = combine_verdicts(base, external)
    assert combined.trade_allowed is False


def test_size_multiplier_out_of_range_rejected():
    import pytest

    with pytest.raises(ValueError):
        ContextVerdict(trade_allowed=True, size_multiplier=Decimal("1.5"), reason="bad")
