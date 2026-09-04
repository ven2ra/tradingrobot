"""Strategy: адаптивная сетка, включается исключительно в режиме range.

Правила:
  * Сетка включается только когда Regime.RANGE (проверка снаружи, но
    стратегия дополнительно отказывает, если её вызвали не в range —
    defense in depth).
  * Шаг сетки = f(volatility, price_step) — не фиксированный процент.
  * Только лимитные заявки.
  * Максимум уровней, максимум инвентаря, запрет докупки против тренда —
    из конфига.
  * Объём уровня: lots = floor(allowed_notional / (price * lot_size)),
    затем проверка min lot (>= 1).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from trading_robot.domain.types import Instrument, InstrumentSpec, LimitOrderRequest, Side, TimeInForce
from trading_robot.regime.regime import Regime


@dataclass(frozen=True, slots=True)
class GridConfig:
    max_levels: int  # максимум уровней сетки на инструмент
    max_inventory_lots: int  # максимум суммарного инвентаря (лотов) по инструменту от сетки
    step_volatility_multiplier: Decimal  # шаг = price_step * max(1, volatility * multiplier / price_step_bps_norm)
    min_step_price_steps: int  # минимальный шаг сетки, выражен в количестве price_step (>=1)
    level_notional_fraction: Decimal  # доля allowed_notional, выделяемая на один уровень


@dataclass(frozen=True, slots=True)
class GridLevel:
    level_index: int  # 0 = ближайший к mid, растёт наружу
    side: Side
    price: Decimal
    client_order_id: str


@dataclass(frozen=True, slots=True)
class GridPlan:
    levels: list[GridLevel]
    reason: str


def compute_grid_step(*, mid_price: Decimal, volatility: Decimal, spec: InstrumentSpec, config: GridConfig) -> Decimal:
    """Шаг сетки зависит от волатильности и шага цены инструмента, а не от
    фиксированного процента цены. volatility — std лог-доходностей (доли).
    """
    vol_component = mid_price * volatility * config.step_volatility_multiplier
    min_step = spec.price_step * config.min_step_price_steps
    raw_step = max(vol_component, min_step)
    steps = (raw_step / spec.price_step).to_integral_value(rounding=ROUND_DOWN)
    steps = max(steps, Decimal(config.min_step_price_steps))
    return steps * spec.price_step


def _stable_client_order_id(instrument: Instrument, level_index: int, side: Side, tick_bucket: str) -> str:
    """Стабильный идемпотентный ключ для логического уровня сетки.

    tick_bucket — например дата торгового дня, чтобы новый день порождал
    новую генерацию заявок, но повторный такт того же дня — нет.
    """
    raw = f"{instrument.key}|{side.value}|L{level_index}|{tick_bucket}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"GRID-{digest}"


def build_grid_plan(
    *,
    instrument: Instrument,
    spec: InstrumentSpec,
    mid_price: Decimal,
    volatility: Decimal,
    regime: Regime,
    current_inventory_lots: int,
    trend_direction: Regime | None,  # Regime.UPTREND / DOWNTREND если сетка уже видела тренд ранее, иначе None
    config: GridConfig,
    tick_bucket: str,
) -> GridPlan:
    if regime != Regime.RANGE:
        return GridPlan(levels=[], reason=f"grid disabled outside range (regime={regime.value})")

    step = compute_grid_step(mid_price=mid_price, volatility=volatility, spec=spec, config=config)

    levels: list[GridLevel] = []
    for i in range(1, config.max_levels + 1):
        buy_price = _round_down_to_step(mid_price - step * i, spec.price_step)
        sell_price = _round_up_to_step(mid_price + step * i, spec.price_step)

        # Запрет докупки против тренда: если инвентарь уже накоплен в одну
        # сторону и последний известный тренд был противоположным, новые
        # уровни в сторону увеличения инвентаря не ставятся.
        allow_buy = current_inventory_lots < config.max_inventory_lots
        if trend_direction == Regime.DOWNTREND and current_inventory_lots > 0:
            allow_buy = False

        allow_sell = current_inventory_lots > -config.max_inventory_lots
        if trend_direction == Regime.UPTREND and current_inventory_lots < 0:
            allow_sell = False

        if allow_buy and buy_price > 0:
            levels.append(
                GridLevel(
                    level_index=i,
                    side=Side.BUY,
                    price=buy_price,
                    client_order_id=_stable_client_order_id(instrument, i, Side.BUY, tick_bucket),
                )
            )
        if allow_sell:
            levels.append(
                GridLevel(
                    level_index=i,
                    side=Side.SELL,
                    price=sell_price,
                    client_order_id=_stable_client_order_id(instrument, i, Side.SELL, tick_bucket),
                )
            )

    return GridPlan(levels=levels, reason="grid built in range regime")


def compute_lots_for_level(*, allowed_notional: Decimal, price: Decimal, spec: InstrumentSpec) -> int:
    if price <= 0 or spec.lot_size <= 0:
        return 0
    lots = (allowed_notional / (price * spec.lot_size)).to_integral_value(rounding=ROUND_DOWN)
    lots_int = int(lots)
    return lots_int if lots_int >= 1 else 0


def level_to_order_request(level: GridLevel, instrument: Instrument, lots: int) -> LimitOrderRequest | None:
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


def _round_down_to_step(price: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return price
    steps = (price / step).to_integral_value(rounding=ROUND_DOWN)
    return steps * step


def _round_up_to_step(price: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return price
    steps = (price / step).to_integral_value(rounding=ROUND_DOWN)
    result = steps * step
    if result < price:
        result += step
    return result
