"""Полный сброс T-Invest sandbox-счёта: закрывает ВСЕ существующие sandbox-
счета пользователя и открывает новый, чистый, с заданным виртуальным
балансом. Только для sandbox — на боевом контуре такой операции нет и
быть не должно (см. TInvestAdapter — там нет `close`-аналога для боевых
счетов, это намеренно).

Зачем отдельный инструмент, а не просто "удалить state.json": сам робот
хранит состояние ЛОКАЛЬНО (state.json/journal.jsonl) — это никак не
влияет на РЕАЛЬНЫЙ (пусть и виртуальный) sandbox-счёт на стороне брокера.
Если на sandbox-счёте уже накопились тестовые позиции/сделки и хочется
начать с чистого листа — единственный способ это сделать полностью
корректно: закрыть счёт и открыть новый (частичного "обнуления"
позиций в API нет).

Запуск (робота предварительно ОСТАНОВИТЕ, чтобы он не работал с
закрываемым счётом параллельно):
  python -m trading_robot.tools.reset_sandbox --config config/config.yaml
  python -m trading_robot.tools.reset_sandbox --config config/config.yaml --initial-cash 500000
"""
from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal

from trading_robot.config.loader import load_config

try:
    from t_tech.invest import Client
    from t_tech.invest.constants import INVEST_GRPC_API_SANDBOX
    from t_tech.invest.utils import decimal_to_money
except ImportError:
    print(
        "пакет t-tech-investments не установлен. Установите:\n"
        "  pip install t-tech-investments --index-url "
        "https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple",
        file=sys.stderr,
    )
    raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Полный сброс T-Invest sandbox-счёта")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument(
        "--initial-cash", type=str, default=None,
        help="Виртуальный баланс нового счёта в рублях (по умолчанию — "
             "TINVEST_SANDBOX_INITIAL_CASH из окружения, иначе 1000000)",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Не спрашивать подтверждение (для использования в скриптах)",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    if cfg.broker.kind != "tinvest" or not cfg.broker.sandbox:
        print(
            f"broker.kind={cfg.broker.kind}, sandbox={cfg.broker.sandbox} — "
            "этот инструмент только для broker.kind: tinvest с sandbox: true. Останов.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    token = os.environ.get(cfg.broker.token_env)
    if not token:
        print(f"переменная окружения {cfg.broker.token_env} не задана", file=sys.stderr)
        raise SystemExit(1)

    initial_cash = Decimal(
        args.initial_cash or os.environ.get("TINVEST_SANDBOX_INITIAL_CASH", "1000000")
    )

    with Client(token, target=INVEST_GRPC_API_SANDBOX) as client:
        accounts = client.sandbox.get_sandbox_accounts().accounts
        if accounts:
            print(f"Найдено sandbox-счетов: {len(accounts)}")
            for acc in accounts:
                print(f"  {acc.id}")
            if not args.yes:
                answer = input("Закрыть ВСЕ перечисленные счета и открыть новый? [y/N] ")
                if answer.strip().lower() not in ("y", "yes", "да"):
                    print("Отменено.")
                    return
            for acc in accounts:
                client.sandbox.close_sandbox_account(account_id=acc.id)
                print(f"Закрыт: {acc.id}")
        else:
            print("Существующих sandbox-счетов нет.")

        new_account = client.sandbox.open_sandbox_account(name="trading-robot")
        client.sandbox.sandbox_pay_in(
            account_id=new_account.account_id,
            amount=decimal_to_money(initial_cash, "rub"),
        )
        print(f"Открыт новый счёт {new_account.account_id}, зачислено {initial_cash} RUB.")

    print(
        "\nГотово. Не забудьте очистить локальное состояние робота перед запуском:\n"
        "  systemctl stop tradingrobot\n"
        "  rm -f data/journal.jsonl data/journal.log data/state.json data/account.json\n"
        "  systemctl start tradingrobot"
    )


if __name__ == "__main__":
    main()
