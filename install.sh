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

# Используем стабильное имя — старый файл просто перезапишется.
# Не используем mktemp+trap, потому что trap срабатывает при exec
# и удаляет файл раньше, чем Python успевает его прочитать.
TARGET="/tmp/traffic-monitor-install.py"

# Список источников: основной + зеркало через CDN (на случай если raw недоступен)
URLS=(
    "https://raw.githubusercontent.com/Madmaxchita/traffic-monitor/main/install.py"
    "https://cdn.jsdelivr.net/gh/Madmaxchita/traffic-monitor@main/install.py"
)

# Скачиваем с таймаутами и форсированным IPv4 (на случай битого IPv6 на сервере).
# -4: только IPv4 — избегаем зависаний на неработающем IPv6
# --connect-timeout 10: максимум 10 секунд на установку соединения
# --max-time 60: общий лимит на скачивание
download_with_curl() {
    curl -4 -fsSL \
         --connect-timeout 10 --max-time 60 \
         "$1" -o "$TARGET"
}

download_with_wget() {
    wget -4 -q --timeout=60 --tries=1 -O "$TARGET" "$1"
}

downloaded=false
for url in "${URLS[@]}"; do
    echo "⬇️  Скачиваю установщик с $url ..."
    if command -v curl >/dev/null 2>&1; then
        if download_with_curl "$url"; then
            downloaded=true
            break
        fi
    elif command -v wget >/dev/null 2>&1; then
        if download_with_wget "$url"; then
            downloaded=true
            break
        fi
    else
        apt install -y -qq curl
        if download_with_curl "$url"; then
            downloaded=true
            break
        fi
    fi
    echo "  ⚠ Не удалось — пробую следующий источник"
done

if [[ "$downloaded" != "true" ]]; then
    echo "✗ Не удалось скачать установщик ни из одного источника."
    echo "  Попробуйте склонировать репозиторий вручную:"
    echo "  cd /opt && git clone https://github.com/Madmaxchita/traffic-monitor.git"
    echo "  sudo python3 /opt/traffic-monitor/install.py"
    exit 1
fi

# Базовая проверка, что скачался Python-скрипт, а не страница 404
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
