# MOEX Trading Robot

Автономное Python-приложение торгового робота для Московской биржи.
Приоритеты (по убыванию): сохранность капитала → прозрачность логики →
устойчивость к обрыву связи → простота стратегии. Робот не гарантирует
прибыль.

## 1. Поток решения за один такт

1. `sync_state()` у брокера, приведение внутреннего состояния к брокеру.
2. **Risk (вход №1):** проверка соединения, дневного лимита убытка, времени
   входа (после `entry_start`, не позже `entry_cutoff_min` до закрытия),
   резерва кэша, лимита числа инструментов. При отказе — `skip` в журнал.
3. Для каждого инструмента: расчёт **Features** (mid/spread/volatility/...),
   свежесть данных (`age_of_data_ms` vs порог).
4. **Regime**: классификация по явным порогам (`uptrend/downtrend/range/
   shock/low_liquidity/uncertain`). Входы запрещены при `shock|
   low_liquidity|uncertain`.
5. **ContextFilter**: календарь событий (дивиденды/отчётность/ЦБ) + опционально
   внешний LLM-вердикт (строго `{"trade_allowed", "size_multiplier",
   "reason"}`, без доступа к брокеру). `trade_allowed=false` → `skip`.
6. **Strategy** (сетка): включается только в `range`; шаг = f(volatility,
   price_step); запрет докупки против последнего известного тренда.
7. Расчёт объёма уровня: `lots = floor(allowed_notional / (price *
   lot_size))`, отсечение `lots < 1`; для FORTS — проверка ГО.
8. **Execution**: только лимитные заявки с идемпотентным
   `client_order_id`; рыночные — исключительно аварийный flatten при
   `risk.emergency_market_flatten: true`.
9. **Journal**: каждое решение — одна строка JSONL + человекочитаемый лог.

Порядок фильтров строго: **соединение и Risk → Regime и ContextFilter →
Strategy → расчёт объёма → Execution.**

## 2. Структура файлов

```
trading_robot/
  config/config.example.yaml   # шаблон с дефолтами, в git
  config/config.yaml           # реальный конфиг — НЕ в git, см. раздел 3
  src/trading_robot/
    domain/types.py         # Decimal-типы: Quote, OrderBook, Bar, InstrumentSpec, AccountState, ...
    interfaces/broker.py    # Protocol BrokerAdapter
    brokers/
      mock_broker.py        # детерминированный paper-брокер
      tinvest_adapter.py    # реальный адаптер T-Invest (t_tech.invest, см. раздел 4)
    features/features.py
    regime/regime.py
    context/
      context_filter.py     # календарный ContextFilter
      llm_context.py        # опциональный LLM-вердикт, без доступа к брокеру
    strategy/grid_strategy.py
    risk/risk_manager.py
    journal/journal.py
    store/state_store.py
    engine/loop.py           # главный sync-цикл
    config/loader.py         # pydantic v2 модели + YAML
    main.py
  tests/
    conftest.py
    test_lot_calc.py
    test_grid_regime.py
    test_risk_gates.py        # cash reserve, max instruments, connection loss, daily stop
    test_idempotent_order.py
    test_context_filter.py
```

## 3. Конфигурация

Шаблон — `config/config.example.yaml` (в git, с дефолтами). Реальный
рабочий конфиг — `config/config.yaml`: он **не в git**
(`.gitignore: /config/config.yaml`) специально, чтобы `git reset --hard`
при обновлении (`deploy.sh`) не затирал ваши правки — брокера, часы
сессии, список инструментов и т.п. При первом деплое `deploy.sh`
копирует `config.example.yaml` → `config.yaml`; при повторных —
оставляет `config.yaml` как есть. Правите только `config.yaml`, в
`config.example.yaml` — только если меняете дефолты для будущих
разворачиваний. Ключи, не заданные явно в постановке задачи,
помечены `# DEFAULT` — используются значения из ТЗ (например
`max_daily_loss_pct: 1%`, `cash_reserve: 10000`, `max_instruments: 3`,
`entry_start: 10:05 MSK`). Секреты — только имена переменных окружения
(`TINVEST_TOKEN`, `TINVEST_ACCOUNT_ID`, `LLM_API_KEY`), никогда значения.

## 4. Брокер

`broker.kind` по умолчанию `mock` (полный `MockBroker`: детерминированный
стакан по seed, исполнение лимиток при пересечении цены с
проскальзыванием в bps, деньги/позиции/заявки в памяти). Лот и шаг цены
для `MockBroker` берутся из `instruments[].lot_size`/`price_step` в
конфиге (по умолчанию 10 и 0.01 — уточните под реальную бумагу).

`broker.kind: tinvest` включает реализованный `TInvestAdapter` поверх
официального SDK T-Банка. **Важно:** старый пакет `tinkoff-investments`
(модуль `tinkoff.invest`) снят с PyPI, репозиторий
github.com/RussianInvestments/invest-python архивирован — Т-Банк
переехал на свой GitLab и переименовал пакет в `t-tech-investments`
(модуль `t_tech.invest`), сигнатуры методов при этом не изменились
(проверено живым импортом). Установка:
```bash
pip install 't-tech-investments' --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple
# либо через extras проекта (нужен ещё --extra-index-url):
pip install -e '.[tinvest]' --extra-index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple
```
Методы (`get_quote`, `get_orderbook`, `get_bars`, `get_instrument_spec`,
`get_account`, `place_limit_order`, `cancel_order`) реализованы через
сервисы SDK (`MarketDataService`, `InstrumentsService`,
`OperationsService`/`SandboxService`, `OrdersService`, `UsersService`),
сигнатуры сверены `inspect.signature()` на реально установленном пакете.
Токен и account_id — только из переменных окружения (`broker.token_env`,
`broker.account_id_env` в конфиге называют имена, не значения; сами
имена `TINVEST_TOKEN`/`TINVEST_ACCOUNT_ID` — наш выбор, к названию
пакета отношения не имеют). `broker.sandbox: true` — официальная
песочница T-Invest (реальный контур брокера в изолированном режиме, НЕ
путать с нашим internal `MockBroker`); она работает через отдельный
`SandboxService`, а не `Users/Operations/OrdersService`. Каждый
сетевой вызов SDK обёрнут таймаутом (10с по умолчанию) — зависший
gRPC-вызов превращается в понятную ошибку в журнале, а не в вечную
тишину. Один TODO явно помечен в коде (`used_margin` через
`UsersService.get_margin_attributes` — поля ответа не проверялись).
Перед боевым использованием прогоните smoke-test на `sandbox: true` и
сверьте результат с личным кабинетом.

Не смешивайте методы разных брокеров в одном адаптере — для ALOR/Finam/
BCS пишется отдельный класс, реализующий тот же `Protocol BrokerAdapter`.

## 5. Главный цикл

Синхронный (`engine/loop.py`, без asyncio): один брокерский коннект,
инструменты обрабатываются последовательно, частота тактов — секунды.
Узкое место — сетевой I/O одного клиента, не CPU/конкурентность;
asyncio добавил бы сложность (async-обёртки над sync SDK) без выигрыша
при такой частоте.

## 6. Запуск тестов

```bash
pip install -e . pytest pydantic pyyaml
pytest -q
```

Все тесты используют только `MockBroker` — живой API брокера в тестах
не вызывается.

## 7. Деплой на сервер (systemd, изолированно от других проектов)

```bash
sudo bash deploy/deploy.sh
```

Скрипт создаёт системного пользователя `tradingrobot` без права входа,
клонирует репозиторий в `/opt/tradingrobot`, ставит зависимости в venv и
поднимает ДВА `systemd`-сервиса:

- `tradingrobot` — сам движок (см. `deploy/tradingrobot.service`);
- `tradingrobot-web` — read-only веб-панель мониторинга на порту `8765`
  (см. `deploy/tradingrobot-web.service` и раздел 8 ниже).

Оба работают независимо от прочих сайтов/сервисов на хосте, со своими
логами (`journalctl -u tradingrobot -f`, `journalctl -u tradingrobot-web -f`)
и каталогом данных (`/opt/tradingrobot/data`). Секреты (`TINVEST_TOKEN`,
`WEBUI_USER`/`WEBUI_PASSWORD` и т.п.) кладутся в `/etc/tradingrobot.env`
(см. `deploy/tradingrobot.env.example`), а не в `config.yaml`; при первом
запуске `deploy.sh` пароль панели генерируется автоматически и выводится
в консоль. Обновление: повторный запуск `deploy.sh` (git reset --hard на
свежий `main` + перезапуск сервисов).

## 8. Веб-панель мониторинга (read-only)

`src/trading_robot/webui/server.py` — отдельный HTTP-процесс на стандартной
библиотеке Python (без Flask/FastAPI), который читает `journal.jsonl`,
`state.json` и `account.json` движка (последний — снапшот счёта:
кэш/equity/позиции с нереализованным P&L, перезаписывается каждый такт,
см. `journal/account_snapshot.py`) и отдаёт русифицированную HTML-страницу
с автообновлением (раз в 3с): текущий торговый день, капитал сейчас,
изменение с начала дня, суммарный нереализованный P&L, флаг дневного
стоп-лосса, таблицу открытых позиций с P&L по каждой, и последние 200
решений (`enter/skip/cancel/flatten/sync`) с причиной, ценой, лотами,
статусом заявки — плюс сворачиваемый блок-глоссарий и подсказки при
наведении для тех, кто не разбирается в трейдинге. **У панели нет доступа
к `BrokerAdapter`
и она не может выставить/отменить ни одной заявки** — она только читает
файлы, которые пишет `RobotEngine`.

Единственное узкое исключение — блок «Какие акции отслеживать»: панель
пишет `data/selected_instruments.json` (`POST /api/instruments`), движок
перечитывает этот файл на каждом такте (`RobotEngine._load_instruments`)
и подставляет его вместо `config.instruments`. Список для быстрого выбора
берётся из курируемого набора ~115 ликвидных тикеров MOEX
(`src/trading_robot/data/liquid_tickers.py`, `GET /api/universe`), но
можно добавить и любой другой тикер вручную — что бы ни было выбрано,
input/enter/skip по-прежнему решают Regime/ContextFilter/Strategy/Risk,
панель только определяет, ЗА какими бумагами вообще следить. Лимит — 30
тикеров одновременно (каждый — минимум 2 сетевых запроса к брокеру за
такт). Для `MockBroker` тикер, которого не было в `config.instruments`,
получает DEFAULT-спецификацию (лот из курируемого списка или 10,
шаг цены 0.01) — для `tinvest` спецификация всегда берётся у брокера.

Локальный запуск:
```bash
python -m trading_robot.webui.server --config config/config.yaml --host 127.0.0.1 --port 8765
```

Если `--host` не `127.0.0.1`/`localhost` — сервер откажется стартовать без
`WEBUI_USER`/`WEBUI_PASSWORD` (Basic Auth), чтобы журнал сделок не оказался
публично доступен без пароля. На сервере, поднятом через `deploy.sh`, панель
уже слушает `0.0.0.0:8765` с автосгенерированным паролем — откройте
`http://<IP_сервера>:8765/` и введите логин/пароль из `/etc/tradingrobot.env`
(и убедитесь, что порт 8765 открыт в файрволе/security group).

## 8а. Новостная лента

`src/trading_robot/newsfeed/poller.py` — ещё один отдельный процесс: раз в
`news.poll_interval_seconds` (DEFAULT 60с) опрашивает публичные RSS
(DEFAULT — Интерфакс `https://www.interfax.ru/rss`, РБК
`https://rssexport.rbc.ru/rbcnews/news/30/full.rss`, оба проверены вживую)
и пишет `data/news.json`, который читает и показывает веб-панель (блок
«Новости»). Список источников — `news.sources` в `config.yaml`, можно
добавить любой другой RSS/Atom (например e-disclosure.ru/MOEX, если
найдёте у них рабочий RSS-адрес — на момент написания их сайты не отдавали
публичный RSS без браузерных заголовков). Лента — витрина для человека:
робот эти новости не читает и решения по ним не принимает, это отдельный
контур от `ContextFilter`. Запуск: `python -m trading_robot.newsfeed.poller
--config config/config.yaml`; на сервере — сервис `tradingrobot-news`.

## 9. Чеклист запуска на paper

1. `pip install -r requirements.txt` (или `pip install -e .`).
2. Проверить `config/config.yaml`: `trading.mode: paper`, `broker.kind: mock`
   (или иной адаптер, если уже реализован боевой вызов SDK).
3. Задать реальные `instruments`, `board`, при необходимости `expiration`
   для срочного рынка.
4. Проверить пороги `risk.*` под свой капитал (`cash_reserve`,
   `max_instrument_weight`, `max_daily_loss_pct`).
5. Убедиться, что каталог `data/` доступен на запись (журнал, state store).
6. Запустить: `python -m trading_robot.main --config config/config.yaml`.
7. Следить за `data/journal.jsonl` и `data/journal.log` — каждое решение
   (`enter/skip/cancel/flatten/sync`) должно быть объяснимо по `reason`.
8. Перед переключением `trading.mode: live` — заменить `broker.kind` на
   реализованный боевой адаптер (снять все `NotImplementedError`) и
   убрать mock-заглушки лота/шага цены из `main.py::build_mock_broker`.

## 10. Ограничения и то, чего робот сознательно не делает

- Не предсказывает цену и не обещает прибыль — только исполняет жёсткие
  правила сетки в `range`-режиме.
- Не использует мартингейл, геометрическое наращивание объёма или
  бесконечное усреднение — инвентарь ограничен `max_inventory_lots`,
  докупка против тренда запрещена.
- Не использует рыночные заявки, кроме аварийного flatten по явному
  флагу `risk.emergency_market_flatten`.
- Не имеет доступа LLM/ContextFilter к исполнению — только вердикт.
- `MockBroker` — детерминированная симуляция для отладки логики, а не
  оценка прибыльности стратегии (нет геп-рисков, нет реалистичного
  стакана, нет комиссий/налогов).
- `TInvestAdapter` (и любой другой боевой адаптер) — каркас: перед live
  необходимо подставить и вручную сверить реальные вызовы SDK с
  актуальной документацией брокера, ни один метод/поле/enum не
  выдуманы «на глаз».
- Не работает с QUIK/DDE/Lua/Excel — только официальный API брокера.
- Risk-слой — жёсткий барьер: Strategy физически не может выставить
  заявку в обход `check_entries_allowed`/`max_allowed_notional_for_instrument`
  (порядок фильтров зафиксирован в `engine/loop.py`).
- Операционные риски вне кода: качество канала связи до брокера,
  расхождение локальных часов с MSK, устаревание календаря событий
  ContextFilter (требует ручного/автоматического обновления), корректность
  `lot_size`/`price_step`/`initial_margin_per_lot`, отдаваемых брокером.
