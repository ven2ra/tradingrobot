"""Journal — неизменяемый журнал решений.

Каждое решение — одна JSON-строка (JSONL, append-only) + дублируется в
человекочитаемый лог. Журнал не должен содержать токены/секреты — пишущий
обязан передавать только доменные значения, сюда никогда не должен
попасть объект конфигурации/окружения целиком.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trading_robot.domain.types import AccountState

MSK = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True, slots=True)
class JournalEntry:
    time_msk: str
    ticker: str
    regime: str
    action: str  # enter | skip | cancel | flatten | sync | update
    reason: str
    client_order_id: str | None
    broker_order_id: str | None
    price: str | None  # str(Decimal), чтобы не терять точность в JSON
    lots: int | None
    status: str
    account_snapshot_hash: str


def account_snapshot_hash(account: AccountState) -> str:
    """Хэш снапшота счёта для журнала — не сами суммы/токены, а их отпечаток,
    достаточный для сверки консистентности между записями.
    """
    payload = {
        "cash": str(account.cash),
        "positions": sorted(
            [(p.instrument.key, p.lots, str(p.average_price)) for p in account.positions]
        ),
        "orders": sorted(
            [(o.client_order_id, o.status.value, o.filled_lots) for o in account.orders]
        ),
    }
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


class Journal:
    def __init__(self, jsonl_path: Path, human_log_path: Path) -> None:
        self._jsonl_path = jsonl_path
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        human_log_path.parent.mkdir(parents=True, exist_ok=True)

        self._logger = logging.getLogger("trading_robot.journal")
        self._logger.setLevel(logging.INFO)
        if not self._logger.handlers:
            handler = logging.FileHandler(human_log_path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)

    def record(
        self,
        *,
        ticker: str,
        regime: str,
        action: str,
        reason: str,
        account: AccountState,
        client_order_id: str | None = None,
        broker_order_id: str | None = None,
        price: Decimal | None = None,
        lots: int | None = None,
        status: str = "",
        now: datetime | None = None,
    ) -> JournalEntry:
        now = now or datetime.now(MSK)
        entry = JournalEntry(
            time_msk=now.astimezone(MSK).isoformat(),
            ticker=ticker,
            regime=regime,
            action=action,
            reason=reason,
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            price=str(price) if price is not None else None,
            lots=lots,
            status=status,
            account_snapshot_hash=account_snapshot_hash(account),
        )
        self._append(entry)
        return entry

    def _append(self, entry: JournalEntry) -> None:
        line = json.dumps(asdict(entry), ensure_ascii=False, sort_keys=True)
        with self._jsonl_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        self._logger.info(
            "%s | %-8s | %-6s | %-6s | %s | cid=%s bid=%s price=%s lots=%s status=%s",
            entry.time_msk,
            entry.ticker,
            entry.regime,
            entry.action,
            entry.reason,
            entry.client_order_id,
            entry.broker_order_id,
            entry.price,
            entry.lots,
            entry.status,
        )
