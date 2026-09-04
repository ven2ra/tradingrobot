"""Курируемый стартовый список ликвидных акций MOEX (секция TQBR).

Это НЕ строгий топ-N по обороту торгов (надёжных данных по обороту на
момент составления получить не удалось) — это вручную отобранные,
хорошо известные голубые фишки и активно торгуемый второй эшелон,
каждый тикер сверен с реальным списком инструментов MOEX ISS
(https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json),
чтобы не включать несуществующие коды. LOT_SIZE — реальный лот по
данным MOEX на момент составления (может измениться — брокер отдаёт
актуальный лот через get_instrument_spec, это поле используется только
как DEFAULT для MockBroker).

Список редактируется свободно — как правкой этого файла, так и через
веб-панель (см. webui/server.py, /api/instruments): панель принимает
ЛЮБОЙ тикер, не только из этого списка, этот файл — только готовое
меню для быстрого выбора.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LiquidTicker:
    ticker: str
    board: str
    lot_size: int


LIQUID_TQBR_SHARES: tuple[LiquidTicker, ...] = tuple(
    LiquidTicker(ticker=t, board="TQBR", lot_size=lot)
    for t, lot in [
        ("SBER", 1), ("SBERP", 1), ("GAZP", 10), ("LKOH", 1), ("GMKN", 10),
        ("NVTK", 1), ("ROSN", 1), ("TATN", 1), ("TATNP", 1), ("SNGS", 100),
        ("SNGSP", 10), ("MGNT", 1), ("MTSS", 10), ("ALRS", 10), ("CHMF", 1),
        ("NLMK", 10), ("MAGN", 10), ("PLZL", 1), ("PHOR", 1), ("MOEX", 10),
        ("VTBR", 1), ("AFLT", 10), ("AFKS", 100), ("IRAO", 100), ("HYDR", 1000),
        ("FEES", 10000), ("RTKM", 10), ("RTKMP", 10), ("RUAL", 10), ("PIKK", 1),
        ("LSRG", 1), ("ETLN", 1), ("SMLT", 1), ("LENT", 1), ("X5", 1),
        ("OZON", 1), ("YDEX", 1), ("VKCO", 1), ("T", 1), ("SVCB", 100),
        ("CBOM", 100), ("BSPB", 10), ("BSPBP", 100), ("SFIN", 1), ("MVID", 1),
        ("DVEC", 1000), ("MSNG", 1000), ("MSRS", 1000), ("UPRO", 1000), ("TGKA", 100000),
        ("TGKB", 1000000), ("TGKBP", 100000), ("TGKN", 100000), ("DIOD", 100), ("KZOS", 10),
        ("KZOSP", 10), ("NKNC", 10), ("NKNCP", 10), ("KMAZ", 10), ("SGZH", 100),
        ("RNFT", 1), ("BANE", 1), ("BANEP", 1), ("TRNFP", 1), ("TRMK", 10),
        ("CHMK", 1), ("ENPG", 1), ("POSI", 1), ("ASTR", 1), ("WUSH", 1),
        ("HEAD", 1), ("HNFG", 1), ("DELI", 1), ("MDMG", 1), ("RENI", 10),
        ("EUTR", 1), ("ABIO", 10), ("AQUA", 1), ("BELU", 1), ("GCHE", 1),
        ("RAGR", 1), ("NKHP", 10), ("OZPH", 10), ("SOFL", 10), ("DATA", 1),
        ("ZAYM", 10), ("MGKL", 100), ("VSEH", 1), ("LEAS", 1), ("SVAV", 1),
        ("UWGN", 1), ("NMTP", 100), ("FESH", 10), ("FLOT", 10), ("IRKT", 100),
        ("UNAC", 1000), ("MTLR", 1), ("MTLRP", 10), ("VSMO", 1), ("SELG", 10),
        ("RASP", 10), ("KOGK", 1), ("OGKB", 1000), ("MRKC", 1000), ("MRKK", 10),
        ("MRKP", 10000), ("MRKS", 1000), ("MRKU", 10000), ("MRKV", 10000), ("MRKY", 10000),
        ("MRKZ", 10000), ("LSNG", 100), ("LSNGP", 10), ("APRI", 10), ("APTK", 10),
    ]
)

LIQUID_TICKER_SET: frozenset[str] = frozenset(t.ticker for t in LIQUID_TQBR_SHARES)


def lot_size_for(ticker: str) -> int:
    """DEFAULT-лот для MockBroker, если тикер есть в курируемом списке, иначе 10."""
    for item in LIQUID_TQBR_SHARES:
        if item.ticker == ticker:
            return item.lot_size
    return 10
