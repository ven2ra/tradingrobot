"""Точка входа. Запуск: python -m trading_robot.main --config config/config.yaml"""
from __future__ import annotations

import argparse
import logging
from decimal import Decimal
from pathlib import Path

from trading_robot.brokers.mock_broker import MockBroker
from trading_robot.config.loader import RootConfig, load_config
from trading_robot.domain.types import InstrumentSpec
from trading_robot.engine.loop import RobotEngine, instruments_from_config
from trading_robot.interfaces.broker import BrokerAdapter
from trading_robot.journal.journal import Journal
from trading_robot.store.state_store import StateStore


def build_mock_broker(cfg: RootConfig) -> MockBroker:
    """Собирает MockBroker со спецификациями инструментов из config.instruments
    (lot_size/price_step/initial_margin_per_lot per-инструмент — DEFAULT-ы,
    если явно не заданы в YAML). Только для broker.kind: mock.
    """
    instruments = instruments_from_config(cfg)
    specs = {
        inst.key: InstrumentSpec(
            instrument=inst,
            lot_size=ic.lot_size,
            price_step=ic.price_step,
            currency="RUB",
            initial_margin_per_lot=ic.initial_margin_per_lot,
        )
        for inst, ic in zip(instruments, cfg.instruments)
    }
    return MockBroker(specs=specs, initial_cash=Decimal("1000000"))


def build_broker(cfg: RootConfig) -> BrokerAdapter:
    if cfg.broker.kind == "mock":
        return build_mock_broker(cfg)
    if cfg.broker.kind == "tinvest":
        from trading_robot.brokers.tinvest_adapter import TInvestAdapter

        return TInvestAdapter(
            token_env=cfg.broker.token_env,
            account_id_env=cfg.broker.account_id_env,
            sandbox=cfg.broker.sandbox,
        )
    raise NotImplementedError(
        f"broker.kind={cfg.broker.kind} не реализован — доступны 'mock' и 'tinvest'. "
        "Для ALOR/Finam/BCS напишите отдельный класс, реализующий Protocol BrokerAdapter."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="MOEX trading robot")
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    cfg = load_config(args.config)

    if cfg.trading.mode == "live" and cfg.broker.kind == "mock":
        raise SystemExit("trading.mode: live несовместим с broker.kind: mock — это точно ошибка конфига")

    broker = build_broker(cfg)
    journal = Journal(Path(cfg.journal.jsonl_path), Path(cfg.journal.human_log_path))
    store = StateStore(Path(cfg.state_store_path))

    engine = RobotEngine(cfg=cfg, broker=broker, journal=journal, state_store=store)
    engine.run_forever()


if __name__ == "__main__":
    main()
