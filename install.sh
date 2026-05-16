#!/usr/bin/env bash
# Bootstrap для install.py
set -e

# КРИТИЧНО: если запустились через curl|bash, наш stdin — это пайп от curl.
# Вложенные curl/apt могут случайно туда залезть и зависнуть.
# Поэтому сразу же закрываем pipe-stdin и подключаем терминал (или /dev/null).
if [[ ! -t 0 ]]; then
    if [[ -r /dev/tty ]]; then
        exec </dev/tty
    else
        exec </dev/null
    fi
fi

if [[ "$EUID" -ne 0 ]]; then
    echo "Запускайте от root: curl ... | sudo bash"
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "📦 Устанавливаю python3..."
    apt update -qq
    DEBIAN_FRONTEND=noninteractive apt install -y -qq python3
fi

URL="https://raw.githubusercontent.com/Madmaxchita/traffic-monitor/main/install.py"
TARGET="/tmp/traffic-monitor-install.py"

echo "⬇️  Скачиваю установщик..."
if command -v curl >/dev/null 2>&1; then
    curl -fsSL --connect-timeout 10 --max-time 60 "$URL" -o "$TARGET"
elif command -v wget >/dev/null 2>&1; then
    wget -q --timeout=60 -O "$TARGET" "$URL"
else
    apt install -y -qq curl
    curl -fsSL --connect-timeout 10 --max-time 60 "$URL" -o "$TARGET"
fi

if ! head -1 "$TARGET" | grep -q "^#!"; then
    echo "✗ Скачанный файл повреждён или установщик недоступен"
    exit 1
fi
chmod +x "$TARGET"

exec python3 "$TARGET" "$@"
