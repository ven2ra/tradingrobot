"""Точка входа. Запуск: python -m trading_robot.main --config config/config.yaml"""
from __future__ import annotations

import argparse
import logging
from decimal import Decimal
from pathlib import Path

from trading_robot.brokers.mock_broker import MockBroker
from trading_robot.config.loader import load_config
from trading_robot.domain.types import InstrumentSpec
from trading_robot.engine.loop import RobotEngine, instruments_from_config
from trading_robot.journal.journal import Journal
from trading_robot.store.state_store import StateStore


def build_mock_broker(cfg) -> MockBroker:
    """Собирает MockBroker с заглушечными спецификациями инструментов.

    Для реального paper/live использования с {{BROKER}} замените на
    соответствующий адаптер (см. brokers/tinvest_adapter.py).
    """
    instruments = instruments_from_config(cfg)
    specs = {
        inst.key: InstrumentSpec(
            instrument=inst,
            lot_size=10,  # DEFAULT — уточнить у брокера per-инструмент
            price_step=Decimal("0.01"),  # DEFAULT — уточнить у брокера per-инструмент
            currency="RUB",
        )
        for inst in instruments
    }
    return MockBroker(specs=specs, initial_cash=Decimal("1000000"))


def main() -> None:
    parser = argparse.ArgumentParser(description="MOEX trading robot")
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    cfg = load_config(args.config)

    if cfg.broker.kind != "mock":
        raise NotImplementedError(
            f"broker.kind={cfg.broker.kind} требует боевого адаптера — реализуйте вызовы "
            "в brokers/tinvest_adapter.py (или аналогичном) перед запуском вне mock"
        )

    broker = build_mock_broker(cfg)
    journal = Journal(Path(cfg.journal.jsonl_path), Path(cfg.journal.human_log_path))
    store = StateStore(Path(cfg.state_store_path))

    engine = RobotEngine(cfg=cfg, broker=broker, journal=journal, state_store=store)
    engine.run_forever()


if __name__ == "__main__":
    main()
