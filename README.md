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
  config/config.yaml
  src/trading_robot/
    domain/types.py         # Decimal-типы: Quote, OrderBook, Bar, InstrumentSpec, AccountState, ...
    interfaces/broker.py    # Protocol BrokerAdapter
    brokers/
      mock_broker.py        # детерминированный paper-брокер
      tinvest_adapter.py    # каркас под tinkoff.invest, NotImplementedError + TODO
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

См. `config/config.yaml`. Ключи, не заданные явно в постановке задачи,
помечены `# DEFAULT` — используются значения из ТЗ (например
`max_daily_loss_pct: 1%`, `cash_reserve: 10000`, `max_instruments: 3`,
`entry_start: 10:05 MSK`). Секреты — только имена переменных окружения
(`TINVEST_TOKEN`, `TINVEST_ACCOUNT_ID`, `LLM_API_KEY`), никогда значения.

## 4. Брокер

`broker.kind` по умолчанию `mock` (полный `MockBroker`: детерминированный
стакан по seed, исполнение лимиток при пересечении цены с
проскальзыванием в bps, деньги/позиции/заявки в памяти). Каркас
`TInvestAdapter` подготовлен под официальный SDK `tinkoff-investments`
(пакет `tinkoff.invest`, https://github.com/RussianInvestments/invest-python) —
каждый метод, где точный вызов SDK не подтверждён, поднимает
`NotImplementedError` с комментарием, какой официальный RPC/сервис
подставить. Не смешивайте методы разных брокеров в одном адаптере —
для ALOR/Finam/BCS пишется отдельный класс, реализующий тот же
`Protocol BrokerAdapter`.

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
библиотеке Python (без Flask/FastAPI), который читает `journal.jsonl` и
`state.json` движка и отдаёт HTML-страницу с автообновлением (раз в 3с):
текущий торговый день, equity на начало дня, флаг дневного стоп-лосса и
последние 200 решений (`enter/skip/cancel/flatten/sync`) с причиной,
ценой, лотами, статусом заявки. **У панели нет доступа к `BrokerAdapter`
и она не может выставить/отменить ни одной заявки** — она только читает
файлы, которые пишет `RobotEngine`.

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
