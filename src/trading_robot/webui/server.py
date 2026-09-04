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
<title>Trading Robot Monitor</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 24px;
         background: #0b0d10; color: #e6e6e6; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .sub { color: #9aa0a6; font-size: 13px; margin-bottom: 20px; }
  .cards { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
  .card { background: #16191d; border: 1px solid #2a2e33; border-radius: 8px; padding: 12px 16px; min-width: 160px; }
  .card .label { font-size: 11px; color: #9aa0a6; text-transform: uppercase; letter-spacing: .04em; }
  .card .value { font-size: 20px; margin-top: 4px; }
  .stopped { color: #ff6b6b; }
  .ok { color: #5ecb7d; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #2a2e33; white-space: nowrap; }
  th { color: #9aa0a6; font-weight: 500; }
  tr.enter { color: #5ecb7d; }
  tr.skip { color: #9aa0a6; }
  tr.cancel, tr.flatten { color: #ffb86b; }
  tr.sync { color: #6ea8fe; }
  .wrap { overflow-x: auto; border: 1px solid #2a2e33; border-radius: 8px; }
  .stale { color: #ff6b6b; font-size: 12px; margin-top: 8px; }
</style>
</head>
<body>
  <h1>Trading Robot &mdash; монитор (read-only)</h1>
  <div class="sub">Панель не исполняет и не отменяет заявки, только читает журнал решений робота.</div>
  <div class="cards" id="cards"></div>
  <div class="wrap">
    <table>
      <thead><tr>
        <th>Время (MSK)</th><th>Тикер</th><th>Режим</th><th>Действие</th><th>Причина</th>
        <th>Цена</th><th>Лоты</th><th>Статус</th><th>client_order_id</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
  <div class="stale" id="stale"></div>

<script>
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
    ['Торговый день', st.trading_day ?? '—'],
    ['Equity на начало дня', st.day_start_equity ?? '—'],
    ['Дневной стоп', st.daily_stopped_out ? 'ДА — только flatten/wait' : 'нет'],
  ];
  document.getElementById('cards').innerHTML = cards.map(([label, value], i) => `
    <div class="card"><div class="label">${label}</div>
      <div class="value ${i === 2 ? (st.daily_stopped_out ? 'stopped' : 'ok') : ''}">${value}</div>
    </div>`).join('');

  const rows = (data.recent || []).slice().reverse().map(e => `
    <tr class="${e.action}">
      <td>${e.time_msk ?? ''}</td>
      <td>${e.ticker ?? ''}</td>
      <td>${e.regime ?? ''}</td>
      <td>${e.action ?? ''}</td>
      <td>${e.reason ?? ''}</td>
      <td>${e.price ?? ''}</td>
      <td>${e.lots ?? ''}</td>
      <td>${e.status ?? ''}</td>
      <td>${e.client_order_id ?? ''}</td>
    </tr>`).join('');
  document.getElementById('rows').innerHTML = rows || '<tr><td colspan="9">Журнал пуст</td></tr>';
}
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
