"""Динамический список отслеживаемых акций.

Файл selected_instruments.json — единственное, что веб-панель имеет
право ЗАПИСЫВАТЬ (узкое, осознанное исключение из read-only: панель
может выбрать, КАКИЕ бумаги торговать, но не МОЖЕТ выставить/отменить
ни одной заявки — это по-прежнему делает только RobotEngine через
Risk/Strategy). Движок перечитывает файл на каждом такте (дешёвая
операция) и подставляет его как список инструментов вместо/поверх
config.instruments.

Валидация тикера здесь намеренно широкая (формат кода MOEX), а не
привязана к курируемому списку liquid_tickers.py — тот только
готовое меню для UI, сам список может содержать любой тикер, который
реально существует на бирже (следующий такт робота получит понятную
ошибку/skip в журнале, если тикер не резолвится брокером).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,14}$")
MAX_INSTRUMENTS = 30  # см. RiskLimits.max_instruments для факт. лимита одновременных позиций;
# это отдельный потолок на размер СПИСКА отслеживаемых (не все обязательно будут в позиции),
# ограничивает время одного такта (каждый инструмент — минимум 2 сетевых вызова к брокеру).


class InstrumentSelectionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SelectedInstrument:
    ticker: str
    board: str = "TQBR"
    instrument_class: str = "share"


def validate_tickers(tickers: list[str]) -> list[str]:
    if len(tickers) > MAX_INSTRUMENTS:
        raise InstrumentSelectionError(f"максимум {MAX_INSTRUMENTS} тикеров за раз")
    cleaned: list[str] = []
    seen = set()
    for raw in tickers:
        t = raw.strip().upper()
        if not t:
            continue
        if not _TICKER_RE.match(t):
            raise InstrumentSelectionError(f"некорректный формат тикера: {raw!r}")
        if t not in seen:
            seen.add(t)
            cleaned.append(t)
    if not cleaned:
        raise InstrumentSelectionError("список не может быть пустым")
    return cleaned


class InstrumentSelectionStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[SelectedInstrument] | None:
        """None означает "файла нет" — вызывающий должен взять список из config.instruments."""
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        items = data.get("instruments", [])
        result = []
        for item in items:
            try:
                result.append(SelectedInstrument(**item))
            except TypeError:
                continue  # битая запись — пропускаем, не роняем движок
        return result or None

    def save(self, tickers: list[str], board: str = "TQBR", instrument_class: str = "share") -> None:
        cleaned = validate_tickers(tickers)
        payload = {
            "instruments": [
                asdict(SelectedInstrument(ticker=t, board=board, instrument_class=instrument_class))
                for t in cleaned
            ]
        }
        raw = json.dumps(payload, ensure_ascii=False, indent=2)
        fd, tmp_path = tempfile.mkstemp(dir=str(self._path.parent), prefix=".instruments-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
