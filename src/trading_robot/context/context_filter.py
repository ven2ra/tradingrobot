"""ContextFilter — внешний контекст: календарь событий, отчётность,
дивиденды, заседания ЦБ, опциональный вердикт внешней модели.

ContextFilter НЕ имеет доступа к BrokerAdapter.place_*/cancel_* — он
только выносит вердикт, исполнение остаётся за Engine/Risk/Execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from trading_robot.domain.types import Instrument


@dataclass(frozen=True, slots=True)
class ContextVerdict:
    trade_allowed: bool
    size_multiplier: Decimal  # в [0.0, 1.0]
    reason: str

    def __post_init__(self) -> None:
        if not (Decimal("0") <= self.size_multiplier <= Decimal("1")):
            raise ValueError("size_multiplier must be within [0.0, 1.0]")


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    instrument_ticker: str | None  # None = общерыночное событие (например заседание ЦБ)
    event_date: date
    kind: str  # "dividend" | "earnings" | "cb_meeting" | "other"
    blackout_days_before: int = 0
    blackout_days_after: int = 0


class ExternalVerdictProvider(Protocol):
    """Опциональный внешний модуль (например LLM-вердикт).

    Реализация ОБЯЗАНА возвращать только ContextVerdict и не иметь
    доступа к брокеру. Единственный допустимый формат ответа модели —
    JSON {"trade_allowed": bool, "size_multiplier": number, "reason": string}.
    """

    def get_verdict(self, instrument: Instrument, as_of: date) -> ContextVerdict:
        ...


class CalendarContextFilter:
    """Базовый ContextFilter на статичном календаре событий (без LLM)."""

    def __init__(self, events: list[CalendarEvent]) -> None:
        self._events = events

    def evaluate(self, instrument: Instrument, as_of: date) -> ContextVerdict:
        for event in self._events:
            if event.instrument_ticker not in (None, instrument.ticker):
                continue
            days_diff = (event.event_date - as_of).days
            in_blackout = -event.blackout_days_after <= days_diff <= event.blackout_days_before
            if in_blackout:
                return ContextVerdict(
                    trade_allowed=False,
                    size_multiplier=Decimal("0"),
                    reason=f"blackout: {event.kind} on {event.event_date.isoformat()}",
                )
        return ContextVerdict(trade_allowed=True, size_multiplier=Decimal("1"), reason="no blocking events")


def combine_verdicts(base: ContextVerdict, external: ContextVerdict | None) -> ContextVerdict:
    """Комбинирует базовый (календарный) и внешний (LLM) вердикты.

    trade_allowed = AND обоих. size_multiplier = min обоих (консервативно).
    """
    if external is None:
        return base
    return ContextVerdict(
        trade_allowed=base.trade_allowed and external.trade_allowed,
        size_multiplier=min(base.size_multiplier, external.size_multiplier),
        reason=f"{base.reason}; llm: {external.reason}",
    )
