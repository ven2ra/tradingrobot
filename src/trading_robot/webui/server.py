"""Read-only веб-панель мониторинга робота.

Отдельный процесс от RobotEngine: читает `journal.jsonl` и `state.json`,
которые пишет движок, и не имеет доступа ни к BrokerAdapter, ни к
`place_*`/`cancel_*` — панель не может влиять на торговлю, только
показывает её. Реализована на стандартной библиотеке (без FastAPI/Flask),
чтобы не тянуть лишние зависимости в прод для простого read-only вьюера.

Запуск:
  python -m trading_robot.webui.server --config config/config.yaml --host 0.0.0.0 --port 8765

Если сервис слушает не только 127.0.0.1, ОБЯЗАТЕЛЬНО задайте
WEBUI_USER/WEBUI_PASSWORD (переменные окружения) — иначе сервер
откажется биндиться на внешний адрес без базовой аутентификации.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from trading_robot.config.loader import RootConfig, load_config

INDEX_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Монитор торгового робота</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 24px;
         background: #0b0d10; color: #e6e6e6; line-height: 1.45; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  h2 { font-size: 15px; margin: 0 0 10px; }
  .sub { color: #9aa0a6; font-size: 13px; margin-bottom: 18px; }

  .hint { display: inline-flex; align-items: center; justify-content: center; width: 15px; height: 15px;
          border-radius: 50%; background: #2a2e33; color: #9aa0a6; font-size: 10px; font-weight: 700;
          margin-left: 5px; cursor: help; vertical-align: middle; }
  .hint:hover { background: #3a3f46; color: #e6e6e6; }

  .panel { background: #16191d; border: 1px solid #2a2e33; border-radius: 10px; padding: 16px 18px; margin-bottom: 18px; }
  .panel-toggle { display: flex; align-items: center; justify-content: space-between; cursor: pointer; user-select: none; }
  .panel-toggle .arrow { transition: transform .15s; color: #9aa0a6; font-size: 12px; }
  .panel.collapsed .panel-body { display: none; }
  .panel.collapsed .arrow { transform: rotate(-90deg); }

  dl.glossary { display: grid; grid-template-columns: max-content 1fr; gap: 6px 16px; margin: 12px 0 0; font-size: 13px; }
  dl.glossary dt { color: #6ea8fe; font-weight: 600; white-space: nowrap; }
  dl.glossary dd { margin: 0; color: #c9ccd1; }

  .legend { display: flex; gap: 16px; flex-wrap: wrap; margin-top: 12px; font-size: 12px; color: #9aa0a6; }
  .legend span.dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 5px; }

  .cards { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 18px; }
  .card { background: #16191d; border: 1px solid #2a2e33; border-radius: 8px; padding: 12px 16px; min-width: 180px; }
  .card .label { font-size: 11px; color: #9aa0a6; text-transform: uppercase; letter-spacing: .04em; }
  .card .value { font-size: 20px; margin-top: 4px; }
  .card .note { font-size: 11px; color: #6a6f76; margin-top: 3px; }
  .stopped { color: #ff6b6b; }
  .ok { color: #5ecb7d; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #2a2e33; white-space: nowrap; }
  th { color: #9aa0a6; font-weight: 500; position: sticky; top: 0; background: #101317; }
  td.reason { white-space: normal; min-width: 260px; }
  .wrap { overflow-x: auto; border: 1px solid #2a2e33; border-radius: 8px; max-height: 70vh; overflow-y: auto; }
  .stale { color: #ff6b6b; font-size: 12px; margin-top: 8px; }

  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; white-space: nowrap; }
  .badge-enter        { background: rgba(94,203,125,.15); color: #5ecb7d; }
  .badge-skip         { background: rgba(154,160,166,.15); color: #9aa0a6; }
  .badge-cancel,
  .badge-flatten      { background: rgba(255,184,107,.15); color: #ffb86b; }
  .badge-sync         { background: rgba(110,168,254,.15); color: #6ea8fe; }

  .badge-range         { background: rgba(94,203,125,.15); color: #5ecb7d; }
  .badge-uptrend       { background: rgba(110,168,254,.15); color: #6ea8fe; }
  .badge-downtrend     { background: rgba(255,140,140,.15); color: #ff8c8c; }
  .badge-shock         { background: rgba(255,107,107,.20); color: #ff6b6b; }
  .badge-low_liquidity { background: rgba(255,184,107,.15); color: #ffb86b; }
  .badge-uncertain,
  .badge-na            { background: rgba(154,160,166,.15); color: #9aa0a6; }

  .status-filled            { color: #5ecb7d; }
  .status-accepted,
  .status-partially_filled  { color: #6ea8fe; }
  .status-rejected,
  .status-cancelled         { color: #ff8c8c; }

  .muted { color: #6a6f76; }
</style>
</head>
<body>
  <h1>Монитор торгового робота</h1>
  <div class="sub">
    Эта страница ничего не покупает и не продаёт — она только показывает, что уже сделал
    (или не сделал) робот, и почему. Все сделки видны здесь с задержкой не больше нескольких секунд.
  </div>

  <div class="panel" id="glossary-panel">
    <div class="panel-toggle" id="glossary-toggle">
      <h2 style="margin:0">📖 Как читать эту панель (для тех, кто не разбирается в трейдинге)</h2>
      <span class="arrow">▼</span>
    </div>
    <div class="panel-body">
      <dl class="glossary">
        <dt>Тикер</dt><dd>Короткий код ценной бумаги на бирже. Например <b>SBER</b> — акции Сбербанка, <b>GAZP</b> — акции Газпрома.</dd>
        <dt>Лот</dt><dd>Минимальная порция, которой торгуют на бирже — не одна акция, а пакет (например 10 штук). Робот всегда считает объём в лотах.</dd>
        <dt>Режим рынка</dt><dd>Робот каждый такт оценивает, что сейчас с ценой, и присваивает один из ярлыков — расшифровка ниже, в цветной легенде под таблицей.</dd>
        <dt>Сетка (grid)</dt><dd>Стратегия робота: он расставляет несколько заявок на покупку чуть ниже текущей цены и на продажу чуть выше — и работает только тогда, когда рынок «стоит на месте» (режим «Флэт»), а не в тренде.</dd>
        <dt>Уровень сетки</dt><dd>Порядковый номер такой заявки: уровень 1 ближе всего к текущей цене, уровень 2 — дальше, и т.д.</dd>
        <dt>Лимитная заявка</dt><dd>Заявка с фиксированной ценой — исполнится только по ней или лучше. Робот <b>никогда</b> не использует рыночные заявки (которые проходят по любой цене), кроме аварийного закрытия позиций.</dd>
        <dt>Капитал (equity)</dt><dd>Деньги на счёте + стоимость всех открытых позиций по текущей цене. Показывает, сколько «стоит» счёт целиком.</dd>
        <dt>Дневной стоп-лосс</dt><dd>Если за день счёт потерял больше заданного процента (обычно 1%), робот перестаёт открывать новые сделки до следующего торгового дня — только закрывает риск и ждёт.</dd>
        <dt>Причина (reason)</dt><dd>Объяснение робота, почему на этом такте он поступил именно так. Технические подробности показаны при наведении курсора.</dd>
      </dl>
    </div>
  </div>

  <div class="cards" id="cards"></div>

  <div class="wrap">
    <table>
      <thead><tr>
        <th>Время (МСК)</th>
        <th>Тикер</th>
        <th>Режим рынка<span class="hint" title="Оценка робота: спокойный рынок, тренд, паника и т.д. Подробности — в легенде под таблицей.">?</span></th>
        <th>Что сделал робот<span class="hint" title="enter — выставил заявку, skip — ничего не сделал (пропустил такт), cancel — отменил заявку, flatten — аварийно закрыл позицию, sync — сверил состояние с брокером при старте.">?</span></th>
        <th>Причина</th>
        <th>Цена</th>
        <th>Лоты<span class="hint" title="Объём заявки в лотах (не в штуках акций — см. пояснение выше).">?</span></th>
        <th>Статус заявки<span class="hint" title="Принята — биржа получила заявку и ждёт исполнения. Исполнена — сделка реально прошла. Отклонена/Отменена — заявки больше нет.">?</span></th>
        <th>ID заявки<span class="hint" title="Внутренний идентификатор робота. Нужен, чтобы повторный запуск робота не продублировал ту же заявку.">?</span></th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>

  <div class="legend">
    <span><span class="dot" style="background:#5ecb7d"></span>Флэт — рынок стоит на месте, сетка работает</span>
    <span><span class="dot" style="background:#6ea8fe"></span>Восходящий тренд — цена растёт, новых сеток нет</span>
    <span><span class="dot" style="background:#ff8c8c"></span>Нисходящий тренд — цена падает, новых сеток нет</span>
    <span><span class="dot" style="background:#ff6b6b"></span>Резкое движение (шок) — сделки запрещены</span>
    <span><span class="dot" style="background:#ffb86b"></span>Низкая ликвидность — сделки запрещены</span>
    <span><span class="dot" style="background:#9aa0a6"></span>Не определено — данных недостаточно, сделки запрещены</span>
  </div>

  <div class="stale" id="stale"></div>

<script>
const REGIME_RU = {
  range: 'Флэт (боковик)',
  uptrend: 'Восходящий тренд',
  downtrend: 'Нисходящий тренд',
  shock: 'Резкое движение (шок)',
  low_liquidity: 'Низкая ликвидность',
  uncertain: 'Не определено',
  'n/a': '—',
};

const ACTION_RU = {
  enter: 'Выставил заявку',
  skip: 'Пропустил такт',
  cancel: 'Отменил заявку',
  flatten: 'Аварийно закрыл позицию',
  sync: 'Сверил состояние',
};

const STATUS_RU = {
  new: 'Новая',
  accepted: 'Принята биржей',
  partially_filled: 'Частично исполнена',
  filled: 'Исполнена',
  cancelled: 'Отменена',
  rejected: 'Отклонена',
  '': '—',
};

const TICKER_RU = {
  SBER: 'Сбербанк',
  GAZP: 'Газпром',
};

// Причины (`reason`) в журнале — технические строки от Python-модулей робота
// (осознанно не локализованы в самом журнале — это неизменяемый машиночитаемый
// лог). Здесь они переводятся только для отображения; оригинал всегда виден
// при наведении курсора на текст причины.
const REASON_RULES = [
  [/^grid level (\d+) (buy|sell)$/, m => `Сетка, уровень ${m[1]}: заявка на ${m[2] === 'buy' ? 'покупку' : 'продажу'}`],
  [/^no broker connection: new entries forbidden$/, () => 'Нет связи с брокером — новые сделки запрещены'],
  [/^daily loss limit reached.*$/, () => 'Достигнут дневной лимит убытка — только закрытие позиций и ожидание до следующего дня'],
  [/^before entry_start (.+)$/, m => `Ещё рано: вход в сделки разрешён только после ${m[1]} по МСК`],
  [/^within (\d+)min of session close$/, m => `Слишком близко к закрытию торгов (менее ${m[1]} мин) — новые сделки не открываются`],
  [/^cash reserve breached.*$/, () => 'Свободных денег меньше неснижаемого резерва — новые сделки запрещены'],
  [/^max_instruments=(\d+) reached$/, m => `Уже торгуется максимум разрешённых инструментов (${m[1]})`],
  [/^entries allowed$/, () => 'Вход в сделки разрешён'],
  [/^regime (\w+) forbids new entries$/, m => `Режим рынка «${REGIME_RU[m[1]] || m[1]}» не допускает новых сделок`],
  [/^market data error: (.+)$/, m => `Не удалось получить рыночные данные: ${m[1]}`],
  [/^context filter: (.+)$/, m => `Внешний фильтр (календарь событий/новости): ${m[1]}`],
  [/^grid disabled outside range \(regime=(\w+)\)$/, m => `Сетка отключена — рынок не во флэте (сейчас: «${REGIME_RU[m[1]] || m[1]}»)`],
  [/^grid built in range regime$/, () => 'Сетка построена — рынок во флэте'],
  [/^computed lots=0.*$/, () => 'Объём сделки округлился до 0 лотов после риск-проверок — заявка не выставлена'],
  [/^insufficient free margin.*$/, () => 'Недостаточно гарантийного обеспечения (ГО) для срочного рынка'],
  [/^margin sufficient$/, () => 'Гарантийного обеспечения достаточно'],
  [/^not a future, margin check n\/a$/, () => 'Не срочный инструмент — проверка ГО не нужна'],
  [/^broker does not report margin.*$/, () => 'Брокер не передаёт данные по ГО — проверка пропущена'],
  [/^no blocking events$/, () => 'В календаре событий нет ничего, что блокирует сделки'],
  [/^blackout: (\w+) on (.+)$/, m => `Запрет на вход по календарю: ${m[1]} ${m[2]}`],
  [/^startup sync_state.*$/, () => 'Запуск робота: состояние сверено с брокером'],
];

function translateReason(text) {
  if (!text) return '—';
  for (const [re, fn] of REASON_RULES) {
    const m = text.match(re);
    if (m) return fn(m);
  }
  return text; // неизвестный формат — показываем как есть
}

function badge(cls, text) {
  return `<span class="badge badge-${cls}">${text}</span>`;
}

async function refresh() {
  let data;
  try {
    const res = await fetch('/api/status', { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    data = await res.json();
  } catch (e) {
    document.getElementById('stale').textContent = 'Нет связи с панелью: ' + e;
    return;
  }
  document.getElementById('stale').textContent = '';

  const st = data.state || {};
  const cards = [
    ['Торговый день', st.trading_day ?? '—', ''],
    ['Капитал на начало дня', st.day_start_equity ?? '—', 'Деньги + позиции, ₽'],
    [
      'Дневной стоп-лосс',
      st.daily_stopped_out ? 'СРАБОТАЛ' : 'не сработал',
      st.daily_stopped_out ? 'Новые сделки запрещены до завтра' : 'Робот торгует как обычно',
    ],
  ];
  document.getElementById('cards').innerHTML = cards.map(([label, value, note], i) => `
    <div class="card"><div class="label">${label}</div>
      <div class="value ${i === 2 ? (st.daily_stopped_out ? 'stopped' : 'ok') : ''}">${value}</div>
      ${note ? `<div class="note">${note}</div>` : ''}
    </div>`).join('');

  const rows = (data.recent || []).slice().reverse().map(e => {
    const regimeCls = (e.regime && e.regime !== 'n/a') ? e.regime : 'na';
    const tickerTitle = TICKER_RU[e.ticker] ? ` title="${TICKER_RU[e.ticker]}"` : '';
    const reasonRu = translateReason(e.reason);
    return `
    <tr>
      <td>${e.time_msk ?? ''}</td>
      <td${tickerTitle}>${e.ticker ?? ''}</td>
      <td>${badge(regimeCls, REGIME_RU[e.regime] ?? e.regime ?? '—')}</td>
      <td>${badge(e.action, ACTION_RU[e.action] ?? e.action ?? '—')}</td>
      <td class="reason" title="${(e.reason ?? '').replace(/"/g, '&quot;')}">${reasonRu}</td>
      <td>${e.price ?? '<span class="muted">—</span>'}</td>
      <td>${e.lots ?? '<span class="muted">—</span>'}</td>
      <td class="status-${e.status || ''}">${STATUS_RU[e.status ?? ''] ?? e.status ?? '—'}</td>
      <td class="muted">${e.client_order_id ?? '—'}</td>
    </tr>`;
  }).join('');
  document.getElementById('rows').innerHTML = rows || '<tr><td colspan="9">Журнал пока пуст — робот ещё не принял ни одного решения.</td></tr>';
}

const glossaryPanel = document.getElementById('glossary-panel');
document.getElementById('glossary-toggle').addEventListener('click', () => {
  glossaryPanel.classList.toggle('collapsed');
  try { localStorage.setItem('glossaryCollapsed', glossaryPanel.classList.contains('collapsed') ? '1' : '0'); } catch (e) {}
});
try {
  if (localStorage.getItem('glossaryCollapsed') === '1') glossaryPanel.classList.add('collapsed');
} catch (e) {}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


def _tail_jsonl(path: Path, limit: int) -> list[dict]:
    if not path.exists():
        return []
    # Файл append-only и обычно небольшой (это журнал решений робота, не
    # тиковые данные) — читаем целиком и берём хвост; для очень больших
    # журналов имеет смысл заменить на посимвольное чтение с конца файла.
    lines = path.read_text(encoding="utf-8").splitlines()
    tail = lines[-limit:]
    result = []
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return result


def _read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def make_handler(cfg: RootConfig, auth_header: str | None):
    journal_path = Path(cfg.journal.jsonl_path)
    state_path = Path(cfg.state_store_path)

    class Handler(BaseHTTPRequestHandler):
        server_version = "TradingRobotMonitor/1.0"

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
            sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

        def _check_auth(self) -> bool:
            if auth_header is None:
                return True
            provided = self.headers.get("Authorization")
            return provided == auth_header

        def _unauthorized(self) -> None:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="trading-robot-monitor"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802 - stdlib method name
            if not self._check_auth():
                self._unauthorized()
                return

            if self.path in ("/", "/index.html"):
                body = INDEX_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path.startswith("/api/status"):
                payload = {
                    "state": _read_state(state_path),
                    "recent": _tail_jsonl(journal_path, limit=200),
                }
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only монитор торгового робота")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    cfg = load_config(args.config)

    user = os.environ.get("WEBUI_USER")
    password = os.environ.get("WEBUI_PASSWORD")
    auth_header: str | None = None
    if user and password:
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        auth_header = f"Basic {token}"
    elif args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "ОТКАЗ: бинд на внешний адрес требует WEBUI_USER и WEBUI_PASSWORD "
            "(переменные окружения) — иначе журнал сделок будет доступен без пароля.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    handler = make_handler(cfg, auth_header)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"trading-robot monitor: http://{args.host}:{args.port} (auth={'on' if auth_header else 'off'})")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
