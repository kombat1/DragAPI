#!/usr/bin/env bash
# Установка DragAPI службой systemd: запуск при загрузке, перезапуск при сбое.
#
#   deploy/install-service.sh --proxy         для домена: HTTPS делает Caddy/nginx (deploy/Caddyfile, nginx.conf),
#                                             платформа слушает только 127.0.0.1
#   deploy/install-service.sh --self-signed   локальная сеть: свой HTTPS платформы, слушает все адреса (0.0.0.0)
#   deploy/install-service.sh --local         только с этой машины: http://127.0.0.1
#
#   ещё: --port 8765   --print (показать службу и ничего не ставить)   --uninstall
#
# Запускайте от обычного пользователя (не root): служба работает от его имени, sudo спросят
# только для записи в /etc/systemd/system.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT=/etc/systemd/system/dragapi.service
MODE=""
PORT=8765
PRINT=0

die() { echo "✗ $*" >&2; exit 1; }
say() { echo "▸ $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --proxy) MODE=proxy ;;
    --self-signed) MODE=self-signed ;;
    --local) MODE=local ;;
    --port) PORT="${2:?--port: нужен номер}"; shift ;;
    --print) PRINT=1 ;;
    --uninstall)
      sudo systemctl disable --now dragapi 2>/dev/null || true
      sudo rm -f "$UNIT" && sudo systemctl daemon-reload
      echo "✓ Служба удалена (данные платформы в backend/data не тронуты)"
      exit 0 ;;
    -h | --help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "неизвестный параметр $1 (см. --help)" ;;
  esac
  shift
done
[[ -n $MODE ]] || die "выберите режим: --proxy, --self-signed или --local (см. --help)"
[[ $PORT =~ ^[0-9]+$ ]] || die "--port: нужен номер"
[[ $(id -u) -ne 0 || $PRINT -eq 1 ]] || die "запустите от обычного пользователя — служба будет работать от его имени"

case "$MODE" in
  proxy) ARGS="--host 127.0.0.1 --port $PORT --behind-proxy"; URL="https://<ваш домен> (через прокси)" ;;
  self-signed) ARGS="--host 0.0.0.0 --port $PORT --tls-self-signed"; URL="https://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT" ;;
  local) ARGS="--host 127.0.0.1 --port $PORT"; URL="http://127.0.0.1:$PORT" ;;
esac

render() {
  sed -e "1,/^$/d" -e "s#@ROOT@#$ROOT#g" -e "s#@USER@#$(id -un)#g" -e "s#@ARGS@#$ARGS#g" "$ROOT/deploy/dragapi.service"
}

if [[ $PRINT -eq 1 ]]; then
  render
  exit 0
fi

command -v systemctl >/dev/null || die "нужен systemd (systemctl)"
say "Ставлю зависимости и собираю интерфейс…"
"$ROOT/start.sh" setup
(cd "$ROOT/frontend" && npm run build --silent >/dev/null) || die "сборка интерфейса не удалась"

TMP="$(mktemp)"
render >"$TMP"
say "Записываю $UNIT (нужен sudo)…"
sudo install -m 644 "$TMP" "$UNIT"
rm -f "$TMP"
sudo systemctl daemon-reload
sudo systemctl enable --now dragapi
sudo systemctl restart dragapi
sleep 3
if systemctl is-active --quiet dragapi; then
  echo "✓ DragAPI работает как служба: $URL"
  echo "  журнал:     journalctl -u dragapi -f"
  echo "  перезапуск: sudo systemctl restart dragapi   (после git pull — сначала $0 --$MODE ещё раз)"
  [[ $MODE == proxy ]] && echo "  дальше: настройте Caddy или nginx — deploy/Caddyfile, deploy/nginx.conf"
  [[ $MODE == self-signed ]] && echo "  сертификат CA для браузеров: $ROOT/backend/data/tls/ca.pem"
  echo "  при первом входе платформа предложит создать администратора"
else
  journalctl -u dragapi -n 30 --no-pager || true
  die "служба не запустилась — журнал выше"
fi
