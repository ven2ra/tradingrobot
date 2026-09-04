"""Опциональный LLM-модуль вердикта по контексту.

Жёсткое ограничение: этот модуль НЕ импортирует BrokerAdapter и не имеет
доступа к place_*/cancel_*. Единственный вход в остальную систему —
ContextVerdict, провалидированный по строгой JSON-схеме. Модель не
считает лоты и не выбирает цену — это делают Strategy и Risk.

Конкретный вызов LLM-провайдера (endpoint, ключ из переменной окружения)
здесь не реализован — сделайте отдельный клиент и передайте сюда как
callable `raw_json_provider`, чтобы не привязывать этот модуль к
конкретному SDK.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Callable

from trading_robot.context.context_filter import ContextVerdict
from trading_robot.domain.types import Instrument

RawJsonProvider = Callable[[Instrument, date], str]

REQUIRED_KEYS = {"trade_allowed", "size_multiplier", "reason"}


class LlmVerdictError(Exception):
    pass


def parse_verdict(raw_json: str) -> ContextVerdict:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise LlmVerdictError(f"invalid JSON from LLM: {exc}") from exc

    if not isinstance(data, dict) or not REQUIRED_KEYS.issubset(data.keys()):
        raise LlmVerdictError(f"LLM response missing required keys {REQUIRED_KEYS}: {data!r}")

    trade_allowed = data["trade_allowed"]
    if not isinstance(trade_allowed, bool):
        raise LlmVerdictError("trade_allowed must be a JSON bool")

    try:
        size_multiplier = Decimal(str(data["size_multiplier"]))
    except (InvalidOperation, TypeError) as exc:
        raise LlmVerdictError("size_multiplier must be a number") from exc

    reason = data["reason"]
    if not isinstance(reason, str):
        raise LlmVerdictError("reason must be a string")

    try:
        return ContextVerdict(trade_allowed=trade_allowed, size_multiplier=size_multiplier, reason=reason)
    except ValueError as exc:
        raise LlmVerdictError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class LlmExternalVerdictProvider:
    """Адаптер: raw_json_provider -> ExternalVerdictProvider (см. context_filter.py)."""

    raw_json_provider: RawJsonProvider
    fail_closed: bool = True  # при ошибке/недоступности LLM — trade_allowed=False

    def get_verdict(self, instrument: Instrument, as_of: date):
        try:
            raw = self.raw_json_provider(instrument, as_of)
            return parse_verdict(raw)
        except Exception as exc:  # noqa: BLE001 - любая ошибка LLM не должна ронять цикл
            if self.fail_closed:
                return ContextVerdict(
                    trade_allowed=False,
                    size_multiplier=Decimal("0"),
                    reason=f"llm unavailable/invalid, fail-closed: {exc}",
                )
            return ContextVerdict(
                trade_allowed=True,
                size_multiplier=Decimal("1"),
                reason=f"llm unavailable, fail-open per config: {exc}",
            )
