# Traffic Monitor — установка

Состоит из двух частей:

- **bot/** — один центральный сервис, ставится на ОДНОЙ машине (можно на любом
  из ваших серверов, главное чтобы все агенты могли до него достучаться по HTTP)
- **agent/** — ставится на КАЖДЫЙ сервер, который надо мониторить

## 1. Установка бота (один раз)

```bash
# Зависимости
apt update
apt install -y python3 python3-pip python3-yaml
pip3 install --break-system-packages aiohttp

# Файлы
mkdir -p /etc/traffic_monitor /var/lib/traffic_monitor /var/log
cp bot/bot.py /usr/local/bin/traffic-bot
chmod +x /usr/local/bin/traffic-bot
cp bot/config.yaml /etc/traffic_monitor/bot.yaml
cp bot/traffic-bot.service /etc/systemd/system/

# Правим конфиг — токен бота, URL self-host API, chat_id, секрет
nano /etc/traffic_monitor/bot.yaml

# Запуск
systemctl daemon-reload
systemctl enable --now traffic-bot
systemctl status traffic-bot
```

**Как узнать свой chat_id:**
1. Напишите боту любое сообщение в Telegram.
2. Выполните:
   ```bash
   curl "${API_BASE}/bot${TOKEN}/getUpdates"
   ```
   В ответе найдите `"chat":{"id":...}` — это и есть ваш chat_id.

**Файрвол на машине с ботом:** откройте порт 8080 (или какой выбрали в
`listen_port`) только для IP ваших серверов-агентов:

```bash
for ip in 1.2.3.4 5.6.7.8; do
    ufw allow from $ip to any port 8080 proto tcp comment 'traffic-bot agent'
done
```

## 2. Установка агента (на каждом сервере)

```bash
# Зависимости
apt update
apt install -y vnstat python3 python3-yaml python3-requests ufw

# vnstat должен быть запущен и накопить хоть какую-то статистику
systemctl enable --now vnstat

# Файлы
mkdir -p /etc/traffic_monitor /var/lib/traffic_monitor
cp agent/agent.py /usr/local/bin/traffic-agent
chmod +x /usr/local/bin/traffic-agent
cp agent/config.yaml /etc/traffic_monitor/agent.yaml
cp agent/traffic-agent.service /etc/systemd/system/

# Правим конфиг — server_id, interface, лимит, URL бота, тот же секрет
nano /etc/traffic_monitor/agent.yaml

# Запуск
systemctl daemon-reload
systemctl enable --now traffic-agent
systemctl status traffic-agent

# Проверка
journalctl -u traffic-agent -f
```

**Важно:** перед тем как доверить агенту блокировку, убедитесь, что в UFW есть
разрешённое правило для вашего SSH-порта. Иначе блокировка может закрыть вам
доступ:

```bash
ufw status numbered
# Если SSH-порта нет — добавить, например:
ufw allow 49222/tcp comment 'SSH'
```

При `auto_block: true` агент при превышении лимита ставит политику
`default deny outgoing` — но явные `allow` правила остаются в силе, так что
SSH работать продолжит (если он добавлен через `ufw allow`).

## 3. Проверка интеграции

На сервере с ботом:

```bash
# Из логов бота должны появиться отчёты:
journalctl -u traffic-bot -f
```

В Telegram отправьте боту `/status` — должен прийти ответ со сводкой.

## 4. Подгонка под себя

- **Пороги уведомлений** — секция `thresholds` в `agent.yaml`, можно править
  на каждом сервере отдельно. Чтобы пороги перевыбрались — удалите
  `/var/lib/traffic_monitor/state.json` и перезапустите агента.
- **День сброса** — `reset_day`, должен совпадать с биллинговым циклом хостинга.
- **Лимит** — `limit_bytes`. Помните, что vnstat считает в TiB (2⁴⁰), а
  хостинги часто в TB (10¹²). 30 TB = 30 000 000 000 000 байт.
- **Только уведомлять, без блокировки** — `auto_block: false`.

## 5. Безопасность

- `shared_secret` — используйте длинную случайную строку, например:
  ```bash
  openssl rand -hex 32
  ```
  Один и тот же на боте и на всех агентах.
- Доступ к HTTP-порту бота должен быть ограничен файрволом — публиковать его
  в открытый интернет не нужно.
- Если бот доступен не по приватной сети, а через интернет — обязательно
  поставьте перед ним HTTPS-прокси (caddy / nginx + Let's Encrypt) и поменяйте
  `bot_url` у агентов на https://.
