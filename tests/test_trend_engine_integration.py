from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from trading_robot.brokers.mock_broker import MockBroker
from trading_robot.config.loader import load_config
from trading_robot.domain.types import LimitOrderRequest, Side, TimeInForce
from trading_robot.engine.loop import RobotEngine
from trading_robot.journal.journal import Journal
from trading_robot.store.state_store import StateStore


@pytest.fixture
def engine(tmp_path: Path, instrument, spec) -> tuple[RobotEngine, MockBroker]:
    cfg = load_config("config/config.example.yaml")
    cfg = cfg.model_copy(
        update={
            "journal": cfg.journal.model_copy(
                update={
                    "jsonl_path": str(tmp_path / "journal.jsonl"),
                    "human_log_path": str(tmp_path / "journal.log"),
                }
            ),
            "state_store_path": str(tmp_path / "state.json"),
            "account_snapshot_path": str(tmp_path / "account.json"),
            "selected_instruments_path": str(tmp_path / "selected_instruments.json"),
        }
    )
    broker = MockBroker(
        specs={instrument.key: spec},
        initial_cash=Decimal("1000000"),
        seed=7,
        base_prices={instrument.key: Decimal("100.00")},
    )
    broker.connect()
    journal = Journal(Path(cfg.journal.jsonl_path), Path(cfg.journal.human_log_path))
    store = StateStore(Path(cfg.state_store_path))
    eng = RobotEngine(cfg=cfg, broker=broker, journal=journal, state_store=store)
    return eng, broker


def _journal_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_manage_trend_exits_closes_position_on_stop_loss(engine, instrument, spec):
    eng, broker = engine

    # Открываем длинную позицию через 100 (заведомо исполнится, mid=100),
    # как будто это уже вход, сделанный trend_strategy на прошлом такте.
    entry = LimitOrderRequest(
        client_order_id="TREND-fake-entry",
        instrument=instrument,
        side=Side.BUY,
        lots=5,
        price=Decimal("110.00"),
        time_in_force=TimeInForce.DAY,
    )
    broker.place_limit_order(entry)
    eng._state.trend_open_instruments = [instrument.key]

    account = broker.get_account()
    position = next(p for p in account.positions if p.instrument.key == instrument.key)
    entry_price = position.average_price

    # Цена ниже стопа (1.5% по умолчанию) -> должна закрыться агрессивной sell-лимиткой.
    stop_mark = entry_price * Decimal("0.90")
    mark_prices = {instrument.key: stop_mark}
    specs = {instrument.key: spec}

    from datetime import datetime, timezone
    eng._manage_trend_exits(account, mark_prices, specs, datetime.now(timezone.utc))

    account_after = broker.get_account()
    position_after = next((p for p in account_after.positions if p.instrument.key == instrument.key), None)
    # Закрывающая лимитка агрессивно пересекает mid и должна была исполниться в MockBroker сразу.
    assert position_after is None or position_after.lots == 0

    entries = _journal_lines(Path(eng._cfg.journal.jsonl_path))
    exit_entries = [e for e in entries if e["action"] == "enter" and "stop-loss" in e["reason"]]
    assert len(exit_entries) == 1

    # Позиция закрылась -> инструмент должен уйти из trend_open_instruments.
    assert instrument.key not in eng._state.trend_open_instruments


def test_manage_trend_exits_holds_when_within_thresholds(engine, instrument, spec):
    eng, broker = engine

    entry = LimitOrderRequest(
        client_order_id="TREND-fake-entry-2",
        instrument=instrument,
        side=Side.BUY,
        lots=5,
        price=Decimal("110.00"),
        time_in_force=TimeInForce.DAY,
    )
    broker.place_limit_order(entry)
    eng._state.trend_open_instruments = [instrument.key]

    account = broker.get_account()
    position = next(p for p in account.positions if p.instrument.key == instrument.key)
    entry_price = position.average_price

    holding_mark = entry_price * Decimal("1.005")  # внутри и стопа, и тейка
    mark_prices = {instrument.key: holding_mark}
    specs = {instrument.key: spec}

    from datetime import datetime, timezone
    eng._manage_trend_exits(account, mark_prices, specs, datetime.now(timezone.utc))

    account_after = broker.get_account()
    position_after = next(p for p in account_after.positions if p.instrument.key == instrument.key)
    assert position_after.lots == position.lots  # позиция не тронута

    assert instrument.key in eng._state.trend_open_instruments  # всё ещё под управлением стратегии
