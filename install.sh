#!/usr/bin/env bash
# Bootstrap для install.py:
# гарантирует Python, скачивает основной установщик,
# и запускает его так, чтобы интерактивные запросы работали даже при curl|bash.
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
# Используем стабильное имя — старый файл просто перезапишется.
# Не используем mktemp+trap, потому что trap срабатывает при exec
# и удаляет файл раньше, чем Python успевает его прочитать.
TARGET="/tmp/traffic-monitor-install.py"

echo "⬇️  Скачиваю установщик..."
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$TARGET"
elif command -v wget >/dev/null 2>&1; then
    wget -qO "$TARGET" "$URL"
else
    apt install -y -qq curl
    curl -fsSL "$URL" -o "$TARGET"
fi

# Базовая проверка, что скачался реальный Python-скрипт, а не HTML-страница 404
if ! head -1 "$TARGET" | grep -q "^#!"; then
    echo "✗ Скачанный файл не похож на Python-скрипт. Содержимое:"
    head -20 "$TARGET"
    exit 1
fi
chmod +x "$TARGET"

# Если stdin не интерактивный (мы под curl|bash) — перенаправим его на терминал,
# чтобы Python-установщик мог читать пользовательский ввод.
if [[ ! -t 0 ]]; then
    if [[ -r /dev/tty ]]; then
        exec </dev/tty
    else
        echo "⚠️  stdin не интерактивный и /dev/tty недоступен."
        echo "   Запустите вручную:"
        echo "   sudo python3 $TARGET"
        exit 1
    fi
fi

exec python3 "$TARGET" "$@"
