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


def test_reconcile_records_fill_status_change(engine, instrument):
    eng, broker = engine

    order = LimitOrderRequest(
        client_order_id="test-reconcile-1",
        instrument=instrument,
        side=Side.BUY,
        lots=1,
        price=Decimal("110.00"),  # заведомо исполнится сразу (mid=100)
        time_in_force=TimeInForce.DAY,
    )
    broker.place_limit_order(order)

    account = broker.get_account()
    eng._reconcile_order_statuses(account)

    entries = _journal_lines(Path(eng._cfg.journal.jsonl_path))
    update_entries = [e for e in entries if e["action"] == "update" and e["client_order_id"] == "test-reconcile-1"]
    assert len(update_entries) == 1
    assert update_entries[0]["status"] == "filled"
    assert update_entries[0]["reason"] == "order status changed to filled"


def test_reconcile_is_idempotent_across_ticks(engine, instrument):
    eng, broker = engine

    order = LimitOrderRequest(
        client_order_id="test-reconcile-2",
        instrument=instrument,
        side=Side.BUY,
        lots=1,
        price=Decimal("110.00"),
        time_in_force=TimeInForce.DAY,
    )
    broker.place_limit_order(order)
    account = broker.get_account()

    eng._reconcile_order_statuses(account)
    eng._reconcile_order_statuses(account)  # тот же статус на следующем такте — новой записи быть не должно

    entries = _journal_lines(Path(eng._cfg.journal.jsonl_path))
    update_entries = [e for e in entries if e["action"] == "update" and e["client_order_id"] == "test-reconcile-2"]
    assert len(update_entries) == 1
