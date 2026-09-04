"""Новостная лента: отдельный процесс, опрашивает публичные RSS-фиды и
пишет news.json, который читает веб-панель.

Не встроено в RobotEngine намеренно: сетевые запросы к новостным сайтам
не должны иметь ни малейшего шанса замедлить или сорвать торговый такт
(тот же принцип, что и у веб-панели — раздельные процессы, движок ничего
не ждёт от других частей системы). Эта лента НЕ используется как вход
для ContextFilter — это отдельная информационная витрина для человека;
если понадобится автоматическая реакция на новости, для этого есть
ContextFilter.evaluate()/ExternalVerdictProvider (см. context/), это
другой, гораздо более ответственный контур, туда попадать напрямую эта
лента не должна.

Источники по умолчанию — официальные бесплатные RSS без ключей:
  * Интерфакс (общая лента, без бизнес-фильтра — такого RSS у них нет
    отдельно, найден и проверен только https://www.interfax.ru/rss)
  * РБК (общая лента, https://rssexport.rbc.ru/rbcnews/news/30/full.rss)
Источники настраиваются в config.yaml (news.sources) — можно добавить
любой другой RSS/Atom, включая e-disclosure.ru, если найдёте у них
рабочий RSS-адрес (на момент написания их сайт отдавал 403 ботам без
браузерных заголовков — проверьте сами).

Разбор RSS/Atom — минимальный, на стандартной библиотеке
(xml.etree.ElementTree), без сторонних зависимостей (feedparser и т.п.).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

from trading_robot.config.loader import NewsConfig, NewsSourceConfig, RootConfig, load_config

logger = logging.getLogger("trading_robot.newsfeed")

_REQUEST_TIMEOUT_SECONDS = 8.0
_USER_AGENT = "Mozilla/5.0 (compatible; trading-robot-newsfeed/1.0)"
_MAX_ITEMS = 150


@dataclass(frozen=True, slots=True)
class NewsItem:
    source: str
    title: str
    link: str
    published: str  # ISO 8601, пусто если фид не отдал дату
    summary: str


def _fetch_raw(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as resp:  # noqa: S310 - фиксированный список URL из конфига
        return resp.read()


def _text(el: ElementTree.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _parse_date(raw: str) -> str:
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return ""


def parse_feed(source_name: str, raw: bytes) -> list[NewsItem]:
    """Разбирает RSS 2.0 (<rss><channel><item>) или Atom (<feed><entry>).

    Минимальный разбор без сторонних библиотек: берём title/link/pubDate
    (RSS) или title/link/updated (Atom) — этого достаточно для витрины
    заголовков, полноценный HTML-контент статьи не парсим и не храним.
    """
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        logger.warning("newsfeed: %s вернул не-XML ответ", source_name)
        return []

    items: list[NewsItem] = []

    # RSS 2.0
    for item_el in root.findall("./channel/item"):
        title = _text(item_el.find("title"))
        link = _text(item_el.find("link"))
        if not title or not link:
            continue
        items.append(
            NewsItem(
                source=source_name,
                title=title,
                link=link,
                published=_parse_date(_text(item_el.find("pubDate"))),
                summary=_text(item_el.find("description"))[:300],
            )
        )

    if items:
        return items

    # Atom
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry_el in root.findall("./atom:entry", ns) or root.findall("entry"):
        title = _text(entry_el.find("atom:title", ns) or entry_el.find("title"))
        link_el = entry_el.find("atom:link", ns) or entry_el.find("link")
        link = link_el.get("href", "") if link_el is not None else ""
        if not title or not link:
            continue
        published_raw = _text(entry_el.find("atom:updated", ns) or entry_el.find("updated"))
        items.append(
            NewsItem(
                source=source_name,
                title=title,
                link=link,
                published=published_raw,  # Atom уже в ISO 8601, доп. парсинг не нужен
                summary=_text(entry_el.find("atom:summary", ns) or entry_el.find("summary"))[:300],
            )
        )
    return items


def poll_once(sources: list[NewsSourceConfig]) -> list[NewsItem]:
    all_items: list[NewsItem] = []
    for src in sources:
        try:
            raw = _fetch_raw(src.url)
            all_items.extend(parse_feed(src.name, raw))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning("newsfeed: не удалось получить %s (%s): %s", src.name, src.url, exc)

    def sort_key(item: NewsItem) -> str:
        return item.published or "0"  # без даты — в конец при сортировке по убыванию

    all_items.sort(key=sort_key, reverse=True)
    return all_items[:_MAX_ITEMS]


def write_news(path: Path, items: list[NewsItem]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "items": [asdict(i) for i in items],
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".news-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def run_forever(cfg: NewsConfig, news_path: Path) -> None:
    logger.info("newsfeed: опрос %d источников каждые %ss -> %s", len(cfg.sources), cfg.poll_interval_seconds, news_path)
    while True:
        try:
            items = poll_once(cfg.sources)
            write_news(news_path, items)
            logger.info("newsfeed: обновлено, %d новостей", len(items))
        except Exception:
            logger.exception("newsfeed: сбой в цикле опроса")
        time.sleep(cfg.poll_interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Новостная лента торгового робота")
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    cfg: RootConfig = load_config(args.config)

    if not cfg.news.enabled:
        logger.info("newsfeed: news.enabled=false в конфиге, процесс завершается")
        return

    run_forever(cfg.news, Path(cfg.news.news_path))


if __name__ == "__main__":
    main()
