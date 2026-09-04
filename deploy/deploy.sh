#!/usr/bin/env bash
# Деплой торгового робота как отдельного systemd-сервиса, изолированно
# от других проектов на сервере (например ffitpro.ru).
#
# Запускать на самом сервере от root:
#   curl -fsSL https://raw.githubusercontent.com/ven2ra/tradingrobot/main/deploy/deploy.sh | bash
# или скопировав репозиторий и выполнив локально: bash deploy/deploy.sh
set -euo pipefail

REPO_URL="https://github.com/ven2ra/tradingrobot"
APP_DIR="/opt/tradingrobot"
SERVICE_NAME="tradingrobot"
SERVICE_USER="tradingrobot"
PYTHON_BIN="python3.11"

echo "== 1/6: системные пакеты =="
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python3"
fi
apt-get update -y
apt-get install -y git "$PYTHON_BIN" "$PYTHON_BIN"-venv

echo "== 2/6: системный пользователь без прав входа =="
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "== 3/6: код =="
if [ -d "$APP_DIR/.git" ]; then
  sudo -u "$SERVICE_USER" git -C "$APP_DIR" fetch origin main
  sudo -u "$SERVICE_USER" git -C "$APP_DIR" reset --hard origin/main
else
  rm -rf "$APP_DIR"
  git clone --depth 1 "$REPO_URL" "$APP_DIR"
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
fi

echo "== 4/6: виртуальное окружение и зависимости =="
sudo -u "$SERVICE_USER" "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -e "$APP_DIR"

echo "== 5/6: каталог данных (журнал, состояние) =="
sudo -u "$SERVICE_USER" mkdir -p "$APP_DIR/data"

echo "== 6/6: systemd unit =="
install -m 0644 "$APP_DIR/deploy/tradingrobot.service" /etc/systemd/system/"$SERVICE_NAME".service
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo
echo "Готово. Статус:"
systemctl --no-pager status "$SERVICE_NAME" || true
echo
echo "Логи:      journalctl -u $SERVICE_NAME -f"
echo "Журнал:    $APP_DIR/data/journal.jsonl / journal.log"
echo "Конфиг:    $APP_DIR/config/config.yaml (после правки: systemctl restart $SERVICE_NAME)"
echo "Секреты:   /etc/tradingrobot.env (создайте вручную, см. deploy/tradingrobot.env.example)"
