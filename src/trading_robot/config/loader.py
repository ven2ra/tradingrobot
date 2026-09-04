"""Загрузка и валидация config.yaml через pydantic v2.

Секреты в конфиге — только ИМЕНА переменных окружения, никогда значения.
"""
from __future__ import annotations

from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class BrokerConfig(BaseModel):
    kind: Literal["mock", "tinvest", "alor", "finam", "bcs"] = "mock"
    token_env: str = "TINVEST_TOKEN"
    account_id_env: str = "TINVEST_ACCOUNT_ID"
    sandbox: bool = True


class InstrumentConfig(BaseModel):
    ticker: str
    board: str  # DEFAULT: TQBR
    instrument_class: Literal["share", "bond", "future"] = "share"
    expiration: str | None = None  # ISO date, только для future
    # Спецификация инструмента для MockBroker (лот/шаг цены у разных бумаг
    # разные — например лот SBER на MOEX равен 10, а у GAZP тоже 10, но у
    # многих других бумаг лот 1 или 100). Для боевого/sandbox брокера
    # (broker.kind != mock) эти поля игнорируются — спецификация приходит
    # напрямую от API брокера через get_instrument_spec().
    lot_size: int = 10  # DEFAULT — уточните реальный лот бумаги у брокера
    price_step: Decimal = Decimal("0.01")  # DEFAULT — уточните реальный шаг цены
    initial_margin_per_lot: Decimal | None = None  # только для FORTS (instrument_class: future)


class FeaturesConfig(BaseModel):
    short_window: int = 5
    mid_window: int = 20
    stale_threshold_ms: int = 5000


class RegimeConfig(BaseModel):
    shock_volatility: Decimal = Decimal("0.02")
    low_liquidity_spread_bps: Decimal = Decimal("50")
    low_liquidity_volume_ratio: Decimal = Decimal("0.3")
    trend_return_threshold: Decimal = Decimal("0.01")
    max_age_ms_for_classification: int = 5000


class StrategyGridConfig(BaseModel):
    max_levels: int = 3
    max_inventory_lots: int = 10
    step_volatility_multiplier: Decimal = Decimal("3")
    min_step_price_steps: int = 2
    level_notional_fraction: Decimal = Decimal("0.1")


class RiskConfig(BaseModel):
    max_daily_loss_pct: Decimal = Decimal("0.01")  # DEFAULT 1%
    max_instrument_weight: Decimal = Decimal("0.20")  # DEFAULT 20%
    cash_reserve: Decimal = Decimal("10000")  # DEFAULT, в валюте счёта
    max_instruments: int = 3  # DEFAULT
    entry_start: time = time(10, 5)  # DEFAULT 10:05 MSK
    entry_cutoff_min: int = 15  # DEFAULT
    emergency_market_flatten: bool = False


class SessionConfig(BaseModel):
    open_time: time = time(10, 0)
    close_time: time = time(18, 45)  # основная сессия TQBR ориентировочно; сверить по календарю MOEX


class JournalConfig(BaseModel):
    jsonl_path: str = "./data/journal.jsonl"
    human_log_path: str = "./data/journal.log"


class LlmConfig(BaseModel):
    enabled: bool = False
    fail_closed: bool = True
    # Имя переменной окружения с API-ключом провайдера LLM (не сам ключ).
    api_key_env: str = "LLM_API_KEY"


class TradingConfig(BaseModel):
    mode: Literal["paper", "live"] = "paper"


class RootConfig(BaseModel):
    trading: TradingConfig = TradingConfig()
    broker: BrokerConfig = BrokerConfig()
    instruments: list[InstrumentConfig] = Field(default_factory=list)
    features: FeaturesConfig = FeaturesConfig()
    regime: RegimeConfig = RegimeConfig()
    strategy: StrategyGridConfig = StrategyGridConfig()
    risk: RiskConfig = RiskConfig()
    session: SessionConfig = SessionConfig()
    journal: JournalConfig = JournalConfig()
    llm: LlmConfig = LlmConfig()
    state_store_path: str = "./data/state.json"
    account_snapshot_path: str = "./data/account.json"
    selected_instruments_path: str = "./data/selected_instruments.json"
    tick_interval_seconds: float = 5.0

    @field_validator("instruments")
    @classmethod
    def _non_empty_in_live(cls, v: list[InstrumentConfig]) -> list[InstrumentConfig]:
        return v


def load_config(path: str | Path) -> RootConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return RootConfig.model_validate(raw or {})
