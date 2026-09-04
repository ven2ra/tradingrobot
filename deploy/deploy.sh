#!/usr/bin/env bash
# Деплой торгового робота (движок + read-only веб-монитор) как отдельных
# systemd-сервисов, изолированно от других проектов на сервере (например
# ffitpro.ru).
#
# Запускать на самом сервере от root:
#   curl -fsSL https://raw.githubusercontent.com/ven2ra/tradingrobot/main/deploy/deploy.sh | bash
# или скопировав репозиторий и выполнив локально: bash deploy/deploy.sh
set -euo pipefail

REPO_URL="https://github.com/ven2ra/tradingrobot"
APP_DIR="/opt/tradingrobot"
SERVICE_NAME="tradingrobot"
WEB_SERVICE_NAME="tradingrobot-web"
NEWS_SERVICE_NAME="tradingrobot-news"
SERVICE_USER="tradingrobot"
PYTHON_BIN="python3.11"
ENV_FILE="/etc/tradingrobot.env"
WEB_PORT="8765"

echo "== 1/7: системные пакеты =="
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python3"
fi
apt-get update -y
apt-get install -y git "$PYTHON_BIN" "$PYTHON_BIN"-venv

echo "== 2/7: системный пользователь без прав входа =="
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "== 3/7: код =="
if [ -d "$APP_DIR/.git" ]; then
  sudo -u "$SERVICE_USER" git -C "$APP_DIR" fetch origin main
  sudo -u "$SERVICE_USER" git -C "$APP_DIR" reset --hard origin/main
else
  rm -rf "$APP_DIR"
  git clone --depth 1 "$REPO_URL" "$APP_DIR"
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
fi

echo "== 4/7: виртуальное окружение и зависимости =="
sudo -u "$SERVICE_USER" "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -e "$APP_DIR"

echo "== 5/7: каталог данных (журнал, состояние) =="
sudo -u "$SERVICE_USER" mkdir -p "$APP_DIR/data"

# config/config.yaml — НЕ в git (см. .gitignore), git reset --hard выше его
# не тронул. При первом деплое копируем из шаблона; если уже существует —
# оставляем как есть (там могут быть ваши правки: broker.kind: tinvest,
# часы сессии, список инструментов и т.п.).
if [ ! -f "$APP_DIR/config/config.yaml" ]; then
  sudo -u "$SERVICE_USER" cp "$APP_DIR/config/config.example.yaml" "$APP_DIR/config/config.yaml"
  echo "config/config.yaml создан из шаблона (config.example.yaml)"
fi

echo "== 6/7: учётные данные веб-монитора =="
# Веб-монитор слушает 0.0.0.0:$WEB_PORT (доступен по IP сервера) и требует
# Basic Auth — без WEBUI_USER/WEBUI_PASSWORD сервис откажется стартовать
# на внешнем адресе. Если /etc/tradingrobot.env ещё нет — создаём с
# сгенерированным паролем; если уже есть — не трогаем (могут быть токены
# брокера/LLM).
GENERATED_NOTICE=""
if [ ! -f "$ENV_FILE" ]; then
  # python3 вместо `tr | head -c`: с `set -o pipefail` то, что head закрывает
  # трубу раньше tr, читается как SIGPIPE-провал всего пайплайна и молча
  # обрывает скрипт (никакого сообщения об ошибке, просто выход).
  WEBUI_PASSWORD_GENERATED="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(15))')"
  cat > "$ENV_FILE" <<EOF
# Секреты робота. НЕ коммитить, НЕ логировать. chmod 600, владелец root.
WEBUI_USER=admin
WEBUI_PASSWORD=$WEBUI_PASSWORD_GENERATED
# TINVEST_TOKEN=...
# TINVEST_ACCOUNT_ID=...
# LLM_API_KEY=...
EOF
  chmod 600 "$ENV_FILE"
  GENERATED_NOTICE="сгенерирован автоматически (см. ниже)"
else
  GENERATED_NOTICE="уже существует, оставлен без изменений"
fi

echo "== 7/7: systemd units =="
install -m 0644 "$APP_DIR/deploy/tradingrobot.service" /etc/systemd/system/"$SERVICE_NAME".service
install -m 0644 "$APP_DIR/deploy/tradingrobot-web.service" /etc/systemd/system/"$WEB_SERVICE_NAME".service
install -m 0644 "$APP_DIR/deploy/tradingrobot-news.service" /etc/systemd/system/"$NEWS_SERVICE_NAME".service
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" "$WEB_SERVICE_NAME" "$NEWS_SERVICE_NAME"
systemctl restart "$SERVICE_NAME" "$WEB_SERVICE_NAME" "$NEWS_SERVICE_NAME"

SERVER_IP="$(curl -fsSL -4 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"

echo
echo "Готово. Статус:"
systemctl --no-pager status "$SERVICE_NAME" || true
systemctl --no-pager status "$WEB_SERVICE_NAME" || true
systemctl --no-pager status "$NEWS_SERVICE_NAME" || true
echo
echo "Логи движка:   journalctl -u $SERVICE_NAME -f"
echo "Логи монитора: journalctl -u $WEB_SERVICE_NAME -f"
echo "Логи новостей: journalctl -u $NEWS_SERVICE_NAME -f"
echo "Журнал:        $APP_DIR/data/journal.jsonl / journal.log"
echo "Конфиг:        $APP_DIR/config/config.yaml (после правки: systemctl restart $SERVICE_NAME)"
echo "Секреты:       $ENV_FILE ($GENERATED_NOTICE)"
echo
echo "Веб-панель:    http://${SERVER_IP}:${WEB_PORT}/  (Basic Auth: см. $ENV_FILE)"
if [ -n "${WEBUI_PASSWORD_GENERATED:-}" ]; then
  echo "  логин:  admin"
  echo "  пароль: $WEBUI_PASSWORD_GENERATED"
fi
echo
echo "Если 8765 порт не открыт наружу — откройте его в файрволе, например:"
echo "  ufw allow ${WEB_PORT}/tcp   # или iptables/security group облака"
