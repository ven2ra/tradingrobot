"""Снапшот состояния счёта для веб-панели.

Отдельный файл от неизменяемого journal.jsonl: это не журнал решений, а
последнее известное состояние счёта (перезаписывается каждый такт), нужен
только для того, чтобы read-only веб-панель могла показать P&L и позиции
БЕЗ доступа к BrokerAdapter — она читает файл, который пишет RobotEngine.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from trading_robot.domain.types import AccountState, InstrumentSpec


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    ticker: str
    board: str
    lots: int
    average_price: str  # str(Decimal) — цена за единицу
    mark_price: str  # str(Decimal) — текущая цена за единицу
    market_value: str  # str(Decimal) — mark_price * lot_size * lots
    unrealized_pnl: str  # str(Decimal) — (mark_price - average_price) * lot_size * lots


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    timestamp: str
    cash: str
    currency: str
    equity: str
    positions: list[PositionSnapshot]


def build_snapshot(
    account: AccountState,
    mark_prices: dict[str, Decimal],
    specs: dict[str, InstrumentSpec],
    equity: Decimal,
    now: datetime,
) -> AccountSnapshot:
    positions: list[PositionSnapshot] = []
    for pos in account.positions:
        if pos.lots == 0:
            # Полностью закрытая позиция (0 лотов) — некоторые брокеры (в т.ч.
            # T-Invest) какое-то время всё ещё отдают такую запись в
            # портфеле после флэттенинга; показывать её как "позицию" не
            # имеет смысла, только засоряет панель.
            continue
        spec = specs.get(pos.instrument.key)
        if spec is None:
            continue
        mark = mark_prices.get(pos.instrument.key, pos.average_price)
        market_value = mark * spec.lot_size * pos.lots
        unrealized_pnl = (mark - pos.average_price) * spec.lot_size * pos.lots
        positions.append(
            PositionSnapshot(
                ticker=pos.instrument.ticker,
                board=pos.instrument.board,
                lots=pos.lots,
                average_price=str(pos.average_price),
                mark_price=str(mark),
                market_value=str(market_value),
                unrealized_pnl=str(unrealized_pnl),
            )
        )
    return AccountSnapshot(
        timestamp=now.isoformat(),
        cash=str(account.cash),
        currency=account.currency,
        equity=str(equity),
        positions=positions,
    )


def write_account_snapshot(path: Path, snapshot: AccountSnapshot) -> None:
    """Атомарная запись (temp-файл + os.replace), как в store/state_store.py."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(snapshot), ensure_ascii=False, sort_keys=True, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".account-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
