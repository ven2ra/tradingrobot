"""Расчёт признаков по инструменту на каждом такте."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from trading_robot.domain.types import Bar, Quote


@dataclass(frozen=True, slots=True)
class Features:
    mid: Decimal
    last: Decimal
    bid: Decimal
    ask: Decimal
    spread_abs: Decimal
    spread_bps: Decimal
    volatility: Decimal  # std логарифмических доходностей за окно bars, в долях (не bps)
    short_return: Decimal
    mid_return: Decimal
    position_in_range: Decimal  # 0..1, где цена относительно [low, high] окна
    volume_vs_avg: Decimal  # текущий объём / средний объём окна
    age_of_data_ms: int
    is_stale: bool


def compute_features(
    quote: Quote,
    bars: list[Bar],
    short_window: int,
    mid_window: int,
    stale_threshold_ms: int,
    now: datetime | None = None,
) -> Features:
    """bars — история от старых к новым, длина >= mid_window рекомендуется.

    volatility — std логарифмических доходностей закрытия за последние
    short_window баров (простая и объяснимая формула, не EWMA/GARCH).
    """
    now = now or datetime.now(timezone.utc)
    mid = (quote.bid + quote.ask) / Decimal("2")
    spread_abs = quote.ask - quote.bid
    spread_bps = (spread_abs / mid * Decimal("10000")) if mid > 0 else Decimal("0")

    age_ms = int((now - quote.timestamp).total_seconds() * 1000)
    is_stale = age_ms > stale_threshold_ms

    closes = [b.close for b in bars]
    volatility = _std_log_returns(closes[-short_window:]) if len(closes) >= 2 else Decimal("0")

    short_return = _simple_return(closes, short_window)
    mid_return = _simple_return(closes, mid_window)

    window = closes[-mid_window:] if closes else [mid]
    lo, hi = min(window), max(window)
    if hi > lo:
        position_in_range = (mid - lo) / (hi - lo)
        position_in_range = max(Decimal("0"), min(Decimal("1"), position_in_range))
    else:
        position_in_range = Decimal("0.5")

    volumes = [Decimal(b.volume) for b in bars[-mid_window:]] if bars else []
    if volumes:
        avg_vol = sum(volumes) / Decimal(len(volumes))
        current_vol = volumes[-1]
        volume_vs_avg = (current_vol / avg_vol) if avg_vol > 0 else Decimal("1")
    else:
        volume_vs_avg = Decimal("1")

    return Features(
        mid=mid,
        last=quote.last,
        bid=quote.bid,
        ask=quote.ask,
        spread_abs=spread_abs,
        spread_bps=spread_bps,
        volatility=volatility,
        short_return=short_return,
        mid_return=mid_return,
        position_in_range=position_in_range,
        volume_vs_avg=volume_vs_avg,
        age_of_data_ms=age_ms,
        is_stale=is_stale,
    )


def _simple_return(closes: list[Decimal], window: int) -> Decimal:
    if len(closes) < window + 1 or window <= 0:
        return Decimal("0")
    start = closes[-window - 1]
    end = closes[-1]
    if start == 0:
        return Decimal("0")
    return (end - start) / start


def _std_log_returns(closes: list[Decimal]) -> Decimal:
    if len(closes) < 2:
        return Decimal("0")
    returns = []
    for prev, cur in zip(closes, closes[1:]):
        if prev <= 0 or cur <= 0:
            continue
        returns.append(math.log(float(cur) / float(prev)))
    if len(returns) < 2:
        return Decimal("0")
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return Decimal(str(math.sqrt(variance)))
