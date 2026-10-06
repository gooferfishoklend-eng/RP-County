#!/usr/bin/env bash
# One-command install / update of the RP-County bot in Docker.
#   bash <(curl -fsSL https://raw.githubusercontent.com/gooferfishoklend-eng/RP-County/HEAD/install.sh)
# Re-running it updates the code and restarts the bot; .env and data/ are kept.
# It only touches /opt/rp-county and the "rp-county" compose project — other
# containers (e.g. remnanode) are never stopped, pruned or reconfigured.
set -euo pipefail

REPO="gooferfishoklend-eng/RP-County"
DIR="${RP_DIR:-/opt/rp-county}"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null || die "запустите от root или установите sudo"
  SUDO="sudo"
fi

command -v docker >/dev/null || die "Docker не найден. Установите Docker (на сервере с remnanode он уже есть)."
if $SUDO docker compose version >/dev/null 2>&1; then
  COMPOSE="$SUDO docker compose"
elif command -v docker-compose >/dev/null; then
  COMPOSE="$SUDO docker-compose"
else
  die "не найден docker compose (плагин compose v2)"
fi

say "Каталог установки: $DIR"
$SUDO mkdir -p "$DIR"
if command -v git >/dev/null; then
  if [ -d "$DIR/.git" ]; then
    say "Обновляю код…"
    $SUDO git -C "$DIR" pull --ff-only
  else
    say "Скачиваю код…"
    tmp="$(mktemp -d)"
    git clone --depth 1 "https://github.com/$REPO.git" "$tmp/src"
    $SUDO cp -a "$tmp/src/." "$DIR/"
    rm -rf "$tmp"
  fi
else
  say "git не найден — скачиваю архив…"
  tmp="$(mktemp -d)"
  curl -fsSL "https://github.com/$REPO/archive/HEAD.tar.gz" | tar -xz -C "$tmp"
  $SUDO cp -a "$tmp"/*/. "$DIR/"
  rm -rf "$tmp"
fi

ask() {  # ask VAR "prompt" [secret]
  local var="$1" prompt="$2" secret="${3:-}" value="${!1:-}"
  while [ -z "$value" ]; do
    if [ -n "$secret" ]; then
      read -r -s -p "$prompt: " value </dev/tty; echo
    else
      read -r -p "$prompt: " value </dev/tty
    fi
  done
  printf -v "$var" '%s' "$value"
}

if [ ! -f "$DIR/.env" ]; then
  say "Первичная настройка"
  ask BOT_TOKEN "Токен Telegram-бота от @BotFather" secret
  [[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || die "токен бота выглядит неверно"
  if [ -z "${AI_PROVIDER:-}" ]; then
    read -r -p "Провайдер ИИ: 1 — Claude API (Anthropic), 2 — OpenRouter [1]: " choice </dev/tty
    [ "${choice:-1}" = "2" ] && AI_PROVIDER=openrouter || AI_PROVIDER=anthropic
  fi
  if [ "$AI_PROVIDER" = "openrouter" ]; then
    ask OPENROUTER_API_KEY "Ключ OpenRouter (https://openrouter.ai/keys)" secret
    ask AI_MODEL "ID модели OpenRouter (с https://openrouter.ai/models)"
    KEY_LINE="OPENROUTER_API_KEY=$OPENROUTER_API_KEY"
  else
    ask ANTHROPIC_API_KEY "Ключ Claude API (https://console.anthropic.com/)" secret
    AI_MODEL="${AI_MODEL:-claude-opus-5-5}"
    KEY_LINE="ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"
  fi
  umask 077
  printf '%s\n' "BOT_TOKEN=$BOT_TOKEN" "AI_PROVIDER=$AI_PROVIDER" "$KEY_LINE" "AI_MODEL=$AI_MODEL" \
    | $SUDO tee "$DIR/.env" >/dev/null
  $SUDO chmod 600 "$DIR/.env"
else
  say "Найден $DIR/.env — оставляю настройки как есть"
fi

$SUDO mkdir -p "$DIR/data"
$SUDO chown 10001:10001 "$DIR/data"

if [ -f "$DIR/data/geopolitics.db" ]; then
  backup_dir="$DIR/backups"
  $SUDO mkdir -p "$backup_dir"
  target="$backup_dir/geopolitics.db.$(date +%Y%m%d-%H%M%S)"
  snap="$DIR/data/.backup-snapshot.db"
  $SUDO rm -f "$snap"
  # consistent snapshot through SQLite itself while the bot keeps running; plain copy if it is stopped
  if $SUDO docker exec rp-county python -c \
      "import sqlite3; sqlite3.connect('/data/geopolitics.db').execute(\"VACUUM INTO '/data/.backup-snapshot.db'\")" \
      >/dev/null 2>&1 && [ -f "$snap" ]; then
    $SUDO mv "$snap" "$target"
  else
    $SUDO cp -a "$DIR/data/geopolitics.db" "$target"
  fi
  say "Резервная копия базы игры: $target"
  $SUDO sh -c "ls -1t '$backup_dir'/geopolitics.db.* 2>/dev/null | tail -n +11 | xargs -r rm -f"
fi

say "Собираю и запускаю контейнер rp-county (первая сборка ~2–4 минуты)…"
cd "$DIR"
$COMPOSE up -d --build --remove-orphans

sleep 5
$SUDO docker logs --tail 15 rp-county || true
cat <<EOF

✅ Готово. Бот работает в контейнере rp-county и не открывает портов — remnanode не затронут.
   Логи:        docker logs -f rp-county
   Перезапуск:  cd $DIR && docker compose restart
   Остановка:   cd $DIR && docker compose down
   Обновление:  запустите эту же команду установки ещё раз
   Настройки:   $DIR/.env   ·   данные игры: $DIR/data   ·   бэкапы: $DIR/backups
EOF
