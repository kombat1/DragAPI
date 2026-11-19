#!/usr/bin/env bash
# Запуск DragAPI: бэкенд и фронтенд одной командой.
#
#   ./start.sh                 обычный режим: собрать интерфейс (если менялся) и запустить платформу
#                              → http://localhost:8765
#   ./start.sh dev             разработка: бэкенд с автоперезапуском + Vite с мгновенным обновлением
#                              → http://localhost:5173 (Ctrl+C останавливает оба)
#   ./start.sh setup           только поставить / обновить зависимости
#   ./start.sh [dev] ФЛАГИ     флаги передаются серверу: --no-auth, --port 9000, --host 0.0.0.0,
#                              --no-local-agent, --config FILE,
#                              HTTPS: --tls-self-signed | --tls-cert F --tls-key F | --behind-proxy
#                              (запуск на сервере, автозапуск и прокси — deploy/README.md)
#
# Порт интерфейса в режиме разработки: DRAGAPI_FRONT_PORT=5174 ./start.sh dev
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
VENV="$BACKEND/.venv"
PY="$VENV/bin/python"

if [[ -t 1 ]]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; D=$'\e[2m'; N=$'\e[0m'; else B= G= Y= R= D= N=; fi
say()  { echo "${B}▸${N} $*"; }
ok()   { echo "${G}✓${N} $*"; }
warn() { echo "${Y}!${N} $*" >&2; }
die()  { echo "${R}✗ $*${N}" >&2; exit 1; }

MODE=prod
case "${1:-}" in
  dev) MODE=dev; shift ;;
  setup) MODE=setup; shift ;;
  -h|--help|help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac
SERVER_ARGS=("$@")

# порт сервера: из --port, иначе 8765 (как у python -m app)
PORT=8765
for ((i = 0; i < ${#SERVER_ARGS[@]}; i++)); do
  case "${SERVER_ARGS[$i]}" in
    --port) PORT="${SERVER_ARGS[$((i + 1))]:-8765}" ;;
    --port=*) PORT="${SERVER_ARGS[$i]#--port=}" ;;
  esac
done
FRONT_PORT="${DRAGAPI_FRONT_PORT:-5173}"

# https, если включён встроенный HTTPS (флагом или в dragapi.toml)
SCHEME=http
CONF="$BACKEND/dragapi.toml"
for ((i = 0; i < ${#SERVER_ARGS[@]}; i++)); do
  case "${SERVER_ARGS[$i]}" in
    --tls-self-signed | --tls-cert | --tls-cert=*) SCHEME=https ;;
    --config) CONF="${SERVER_ARGS[$((i + 1))]:-$CONF}" ;;
    --config=*) CONF="${SERVER_ARGS[$i]#--config=}" ;;
  esac
done
if [[ $SCHEME == http && -f $CONF ]] && grep -Eq '^[[:space:]]*(tls_self_signed[[:space:]]*=[[:space:]]*true|tls_cert[[:space:]]*=)' "$CONF"; then
  SCHEME=https
fi

# ---------- проверки ----------
command -v python3 >/dev/null || die "нужен Python 3.10+ (python3). macOS: brew install python  ·  Linux: sudo apt install python3 python3-venv"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "нужен Python 3.10 или новее, сейчас $(python3 --version)"
if [[ $MODE != setup || ! -d "$FRONTEND/node_modules" ]]; then
  command -v npm >/dev/null || die "нужен Node.js 18+ (npm) для интерфейса. macOS: brew install node  ·  Linux: https://nodejs.org"
fi

port_busy() {   # кто слушает порт (пусто — свободен)
  if command -v ss >/dev/null; then ss -ltnpH "sport = :$1" 2>/dev/null | head -1; return; fi
  if command -v lsof >/dev/null; then lsof -iTCP:"$1" -sTCP:LISTEN -n -P 2>/dev/null | sed -n 2p; return; fi
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && echo "порт $1 занят"
}
check_port() {
  local who cmd="./start.sh"
  who="$(port_busy "$1" || true)"
  [[ -z $who ]] && return 0
  [[ $MODE == dev ]] && cmd+=" dev"
  if [[ $1 == "$FRONT_PORT" && $MODE == dev && $2 == Vite ]]; then cmd="DRAGAPI_FRONT_PORT=5174 $cmd"; else cmd+=" --port 9000"; fi
  die "порт $1 занят ($2):
  ${who}
  Остановите тот процесс или выберите другой порт: $cmd"
}

# ---------- зависимости ----------
# sha256sum (Linux) или shasum (macOS)
if command -v sha256sum >/dev/null; then SHA=(sha256sum); else SHA=(shasum -a 256); fi
hash_of() { "${SHA[@]}" "$@" 2>/dev/null | "${SHA[@]}" | cut -c1-16; }

setup_backend() {
  if [[ ! -x $PY ]]; then
    say "Создаю окружение Python (backend/.venv)…"
    python3 -m venv "$VENV"
  fi
  local want have
  want="$(hash_of "$BACKEND/requirements.txt")"
  have="$(cat "$VENV/.requirements-hash" 2>/dev/null || true)"
  if [[ $want != "$have" ]]; then
    say "Ставлю зависимости бэкенда…"
    "$PY" -m pip install -q --upgrade pip >/dev/null
    "$PY" -m pip install -q -r "$BACKEND/requirements.txt"
    echo "$want" >"$VENV/.requirements-hash"
    ok "Зависимости бэкенда установлены"
  fi
}

setup_frontend() {
  local want have
  want="$(hash_of "$FRONTEND/package.json" "$FRONTEND"/package-lock.json)"
  have="$(cat "$FRONTEND/node_modules/.dragapi-hash" 2>/dev/null || true)"
  if [[ ! -d $FRONTEND/node_modules || $want != "$have" ]]; then
    say "Ставлю зависимости интерфейса (npm install)…"
    (cd "$FRONTEND" && npm install --no-audit --no-fund --loglevel=error)
    echo "$want" >"$FRONTEND/node_modules/.dragapi-hash"
    ok "Зависимости интерфейса установлены"
  fi
}

build_frontend() {
  local dist="$FRONTEND/dist/index.html"
  if [[ -f $dist ]] && [[ -z "$(find "$FRONTEND/src" "$FRONTEND/index.html" "$FRONTEND"/vite.config.* "$FRONTEND/package.json" -newer "$dist" -print -quit 2>/dev/null)" ]]; then
    ok "Интерфейс собран и не менялся"
    return
  fi
  say "Собираю интерфейс…"
  (cd "$FRONTEND" && npm run build --silent >/dev/null) || die "сборка интерфейса не удалась — запустите: cd frontend && npm run build"
  ok "Интерфейс собран"
}

setup_backend
if command -v npm >/dev/null; then setup_frontend; else warn "npm не найден — интерфейс не установлен"; fi
if [[ $MODE == setup ]]; then
  ok "Всё готово. Запуск: ./start.sh   (разработка: ./start.sh dev)"
  exit 0
fi

# ---------- запуск ----------
if [[ $MODE == prod ]]; then
  build_frontend
  check_port "$PORT" "сервер платформы"
  echo
  echo "${B}DragAPI${N} → ${G}$SCHEME://localhost:$PORT${N}   ${D}(Ctrl+C — остановить)${N}"
  cd "$BACKEND"
  exec "$PY" -m app ${SERVER_ARGS[@]+"${SERVER_ARGS[@]}"}
fi

# dev: два процесса, Ctrl+C или падение любого — останавливаем оба
check_port "$PORT" "бэкенд"
check_port "$FRONT_PORT" "Vite"
PIDS=()
set -m   # у каждого фонового процесса своя группа — останавливаем её целиком (uvicorn --reload, vite, sed)
cleanup() {
  trap - INT TERM EXIT
  for pid in "${PIDS[@]}"; do kill -TERM -- "-$pid" 2>/dev/null || true; done
  sleep 0.5
  for pid in "${PIDS[@]}"; do kill -KILL -- "-$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  echo; ok "Остановлено"
}
trap 'cleanup; exit 0' INT TERM   # Ctrl+C — штатная остановка
trap cleanup EXIT

# префикс строк без «sed -u» (его нет на macOS)
prefix() { while IFS= read -r line; do printf '%s %s\n' "$1" "$line"; done; }
(cd "$BACKEND" && exec "$PY" -m app --reload ${SERVER_ARGS[@]+"${SERVER_ARGS[@]}"} 2>&1 | prefix "${D}[бэкенд]${N}") &
PIDS+=($!)
(cd "$FRONTEND" && DRAGAPI_BACKEND="$SCHEME://127.0.0.1:$PORT" DRAGAPI_FRONT_PORT="$FRONT_PORT" exec npx vite --clearScreen false 2>&1 | prefix "${D}[vite]${N}") &
PIDS+=($!)

echo
echo "${B}DragAPI — режим разработки${N}"
echo "  интерфейс  → ${G}http://localhost:$FRONT_PORT${N}   ${D}(правки в frontend/src — сразу)${N}"
echo "  бэкенд     → $SCHEME://localhost:$PORT   ${D}(перезапускается при правках backend/app)${N}"
echo "  ${D}Ctrl+C — остановить оба${N}"
echo

# ждём, пока завершится любой из двух (wait -n нет в bash 3.2 на macOS)
while kill -0 "${PIDS[0]}" 2>/dev/null && kill -0 "${PIDS[1]}" 2>/dev/null; do sleep 1; done
warn "Один из процессов завершился — останавливаю второй"
exit 1
