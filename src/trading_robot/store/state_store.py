"""Локальное состояние робота, атомарно персистируемое в JSON.

При старте: sync_state() у брокера — источник истины. Внутренний store
приводится к брокеру, а не наоборот; расхождения только логируются в
Journal (см. engine/loop.py).
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RobotState:
    # trading day (YYYY-MM-DD MSK) для которого считается дневной P&L / стоп.
    trading_day: str | None = None
    day_start_equity: str | None = None  # str(Decimal)
    daily_stopped_out: bool = False
    # last known trend regime per instrument key, для запрета докупки против тренда.
    last_trend_by_instrument: dict[str, str] = field(default_factory=dict)
    # какие client_order_id уже были отправлены (для восстановления после рестарта).
    known_client_order_ids: list[str] = field(default_factory=list)


class StateStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> RobotState:
        if not self._path.exists():
            return RobotState()
        with self._path.open("r", encoding="utf-8") as f:
            data: dict[str, Any] = json.load(f)
        return RobotState(**data)

    def save(self, state: RobotState) -> None:
        """Атомарная запись: пишем во временный файл в той же директории и
        делаем os.replace (атомарно на POSIX), чтобы не потерять состояние
        при сбое посреди записи.
        """
        payload = json.dumps(asdict(state), ensure_ascii=False, sort_keys=True, indent=2)
        fd, tmp_path = tempfile.mkstemp(dir=str(self._path.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
