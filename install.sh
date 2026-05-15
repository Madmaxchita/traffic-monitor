#!/usr/bin/env bash
# Bootstrap для install.py — гарантирует Python и запускает Python-установщик
set -e

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
TMP="$(mktemp -t tm-install.XXXXXX.py)"
trap 'rm -f "$TMP"' EXIT

echo "⬇️  Скачиваю установщик..."
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$TMP"
elif command -v wget >/dev/null 2>&1; then
    wget -qO "$TMP" "$URL"
else
    apt install -y -qq curl
    curl -fsSL "$URL" -o "$TMP"
fi

exec python3 "$TMP" "$@"
