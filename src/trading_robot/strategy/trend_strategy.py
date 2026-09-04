"""Strategy: пробойный вход по тренду + фиксированный % стоп-лосс/тейк-профит.

Правила:
  * Работает ТОЛЬКО когда classify_regime() вернул UPTREND/DOWNTREND — для
    range есть отдельная сетка (grid_strategy.py); режимы не пересекаются,
    поэтому эти две стратегии никогда не конкурируют за один и тот же такт.
  * Вход — "пробой": агрессивная (маркетируемая) лимитка, пересекающая
    спред на aggressive_offset_bps, чтобы исполниться сразу, а не ждать в
    стакане (в отличие от сетки, которая специально ставит пассивные
    уровни). Без усреднения: пока по инструменту уже есть НЕНУЛЕВАЯ
    позиция, новых входов не открываем.
  * Выход — фиксированный % от цены входа (position.average_price), не
    ATR/волатильность — простые, предсказуемые пороги. Это software-стоп
    (mental stop): BrokerAdapter поддерживает только лимитки (см.
    interfaces/broker.py), отдельной стоп-заявки бирже не отправляется —
    движок на каждом такте сравнивает mark_price с порогами и, если один
    из них пробит, сам выставляет закрывающую агрессивную лимитку.
  * Один вход и один выход в день на инструмент (идемпотентный
    client_order_id по дате, как у сетки) — если агрессивная лимитка не
    исполнилась в тот же день, повторной попытки в тот день не будет; это
    тот же осознанный компромисс, что и в сетке (см. её докстринг), и на
    ликвидных бумагах из куратор-листа маловероятен на практике.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from trading_robot.domain.types import Instrument, LimitOrderRequest, Position, Side, TimeInForce
from trading_robot.regime.regime import Regime


@dataclass(frozen=True, slots=True)
class TrendConfig:
    entry_notional_fraction: Decimal
    aggressive_offset_bps: Decimal
    stop_loss_pct: Decimal
    take_profit_pct: Decimal


@dataclass(frozen=True, slots=True)
class TrendLevel:
    side: Side
    price: Decimal
    client_order_id: str
    purpose: str  # "entry" | "exit_stop_loss" | "exit_take_profit"


def _stable_client_order_id(instrument: Instrument, purpose: str, tick_bucket: str) -> str:
    raw = f"{instrument.key}|{purpose}|{tick_bucket}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"TREND-{digest}"


def _aggressive_price(mid_price: Decimal, side: Side, offset_bps: Decimal) -> Decimal:
    offset = mid_price * offset_bps / Decimal("10000")
    return mid_price + offset if side == Side.BUY else mid_price - offset


def build_trend_entry(
    *,
    instrument: Instrument,
    regime: Regime,
    mid_price: Decimal,
    current_inventory_lots: int,
    config: TrendConfig,
    tick_bucket: str,
) -> tuple[TrendLevel | None, str]:
    """Возвращает (уровень входа или None, объяснение решения)."""
    if regime not in (Regime.UPTREND, Regime.DOWNTREND):
        return None, f"trend entry skipped: regime={regime.value} is not uptrend/downtrend"
    if current_inventory_lots != 0:
        return None, f"trend entry skipped: already have a position ({current_inventory_lots} lots), no averaging"
    side = Side.BUY if regime == Regime.UPTREND else Side.SELL
    price = _aggressive_price(mid_price, side, config.aggressive_offset_bps)
    level = TrendLevel(
        side=side,
        price=price,
        client_order_id=_stable_client_order_id(instrument, f"entry-{side.value}", tick_bucket),
        purpose="entry",
    )
    return level, f"breakout entry: {side.value} at {price} (regime={regime.value})"


def build_trend_exit(
    *,
    instrument: Instrument,
    position: Position,
    mark_price: Decimal,
    config: TrendConfig,
    tick_bucket: str,
) -> tuple[TrendLevel | None, str]:
    """Возвращает (уровень выхода или None, объяснение решения)."""
    if position.lots == 0:
        return None, "trend exit skipped: no position"
    entry_price = position.average_price
    if entry_price <= 0:
        return None, "trend exit skipped: invalid entry price"

    if position.lots > 0:
        stop_price = entry_price * (Decimal("1") - config.stop_loss_pct)
        target_price = entry_price * (Decimal("1") + config.take_profit_pct)
        if mark_price <= stop_price:
            side, purpose = Side.SELL, "exit_stop_loss"
            reason = f"long stop-loss hit: mark={mark_price} <= stop={stop_price}"
        elif mark_price >= target_price:
            side, purpose = Side.SELL, "exit_take_profit"
            reason = f"long take-profit hit: mark={mark_price} >= target={target_price}"
        else:
            return None, f"long holding: stop={stop_price} < mark={mark_price} < target={target_price}"
    else:
        stop_price = entry_price * (Decimal("1") + config.stop_loss_pct)
        target_price = entry_price * (Decimal("1") - config.take_profit_pct)
        if mark_price >= stop_price:
            side, purpose = Side.BUY, "exit_stop_loss"
            reason = f"short stop-loss hit: mark={mark_price} >= stop={stop_price}"
        elif mark_price <= target_price:
            side, purpose = Side.BUY, "exit_take_profit"
            reason = f"short take-profit hit: mark={mark_price} <= target={target_price}"
        else:
            return None, f"short holding: target={target_price} < mark={mark_price} < stop={stop_price}"

    price = _aggressive_price(mark_price, side, config.aggressive_offset_bps)
    level = TrendLevel(
        side=side,
        price=price,
        client_order_id=_stable_client_order_id(instrument, purpose, tick_bucket),
        purpose=purpose,
    )
    return level, reason


def compute_trend_entry_lots(*, allowed_notional: Decimal, price: Decimal, lot_size: int) -> int:
    if price <= 0 or lot_size <= 0:
        return 0
    lots = (allowed_notional / (price * lot_size)).to_integral_value(rounding=ROUND_DOWN)
    lots_int = int(lots)
    return lots_int if lots_int >= 1 else 0


def trend_level_to_order_request(level: TrendLevel, instrument: Instrument, lots: int) -> LimitOrderRequest | None:
    if lots <= 0:
        return None
    return LimitOrderRequest(
        client_order_id=level.client_order_id,
        instrument=instrument,
        side=level.side,
        lots=lots,
        price=level.price,
        time_in_force=TimeInForce.DAY,
    )
