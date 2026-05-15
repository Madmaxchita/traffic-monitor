#!/usr/bin/env python3
"""
Traffic monitor bot.

Один центральный сервис:
- принимает HTTP-отчёты и уведомления от агентов
- шлёт сообщения в Telegram через кастомный Bot API
- отвечает на команды от пользователя (/status, /servers, ...)
- раз в сутки шлёт сводку по всем серверам

Зависимости:
    pip install aiohttp pyyaml
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from aiohttp import web, ClientSession, ClientTimeout

CONFIG_PATH = Path("/etc/traffic_monitor/bot.yaml")
STATE_PATH = Path("/var/lib/traffic_monitor/bot-state.json")
LOG_PATH = Path("/var/log/traffic_monitor-bot.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("bot")


# ---------- конфиг ----------

@dataclass
class Config:
    telegram_token: str
    telegram_api_base: str
    chat_ids: list[int]
    listen_host: str
    listen_port: int
    shared_secret: str
    daily_report_hour: int
    agent_silence_alert: int

    @classmethod
    def load(cls, path: Path) -> "Config":
        with path.open() as f:
            d = yaml.safe_load(f)
        return cls(
            telegram_token=d["telegram_token"],
            telegram_api_base=d["telegram_api_base"].rstrip("/"),
            chat_ids=[int(x) for x in d["chat_ids"]],
            listen_host=d.get("listen_host", "0.0.0.0"),
            listen_port=int(d.get("listen_port", 8080)),
            shared_secret=d["shared_secret"],
            daily_report_hour=int(d.get("daily_report_hour", 10)),
            agent_silence_alert=int(d.get("agent_silence_alert", 7200)),
        )


# ---------- состояние ----------
# Хранит последний отчёт каждого сервера

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            log.exception("Ошибка чтения состояния")
    return {"servers": {}, "last_silence_alert": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


def fmt_bytes(n: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.2f} {u}"
        f /= 1024
    return f"{n} B"


# ---------- работа с Telegram через self-host API ----------

class TelegramClient:
    def __init__(self, cfg: Config, session: ClientSession):
        self.cfg = cfg
        self.session = session
        self.base = f"{cfg.telegram_api_base}/bot{cfg.telegram_token}"
        self._offset = 0

    async def send_message(self, chat_id: int, text: str) -> None:
        try:
            async with self.session.post(
                f"{self.base}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    log.warning("Telegram sendMessage %s: %s", r.status, await r.text())
        except Exception:
            log.exception("Ошибка отправки в Telegram")

    async def broadcast(self, text: str) -> None:
        for chat_id in self.cfg.chat_ids:
            await self.send_message(chat_id, text)

    async def get_updates(self) -> list[dict]:
        try:
            async with self.session.get(
                f"{self.base}/getUpdates",
                params={"offset": self._offset, "timeout": 30},
                timeout=ClientTimeout(total=40),
            ) as r:
                data = await r.json()
                if not data.get("ok"):
                    return []
                updates = data.get("result", [])
                if updates:
                    self._offset = updates[-1]["update_id"] + 1
                return updates
        except Exception:
            log.exception("Ошибка getUpdates")
            return []


# ---------- HTTP API для агентов ----------

def make_app(cfg: Config, state: dict, tg: TelegramClient) -> web.Application:
    app = web.Application()

    async def check_auth(request: web.Request) -> bool:
        return request.headers.get("X-Auth-Token") == cfg.shared_secret

    async def handle_report(request: web.Request):
        if not await check_auth(request):
            return web.Response(status=401, text="unauthorized")
        data = await request.json()
        sid = data["server_id"]
        state["servers"][sid] = {
            "used_bytes": int(data["used_bytes"]),
            "limit_bytes": int(data["limit_bytes"]),
            "percent": float(data["percent"]),
            "blocked": bool(data["blocked"]),
            "ts": time.time(),
        }
        save_state(state)
        log.info("Отчёт от %s: %.1f%%", sid, data["percent"])
        return web.json_response({"ok": True})

    async def handle_notify(request: web.Request):
        if not await check_auth(request):
            return web.Response(status=401, text="unauthorized")
        data = await request.json()
        sid = data["server_id"]
        text = data["text"]
        level = data.get("level", "info")
        emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🛑"}.get(level, "•")
        msg = f"{emoji} <b>[{sid}]</b>\n{text}"
        await tg.broadcast(msg)
        return web.json_response({"ok": True})

    app.router.add_post("/report", handle_report)
    app.router.add_post("/notify", handle_notify)
    return app


# ---------- обработка команд из Telegram ----------

async def handle_command(cfg: Config, state: dict, tg: TelegramClient,
                         chat_id: int, text: str) -> None:
    if chat_id not in cfg.chat_ids:
        log.warning("Команда из чужого чата %s: %r", chat_id, text)
        return

    parts = text.strip().split()
    cmd = parts[0].split("@")[0].lower()  # /status@botname -> /status

    if cmd == "/status":
        if not state["servers"]:
            await tg.send_message(chat_id, "Пока нет данных ни от одного сервера.")
            return
        lines = ["<b>Статус серверов:</b>"]
        now = time.time()
        for sid, s in sorted(state["servers"].items()):
            age = int(now - s["ts"])
            mark = "🛑" if s["blocked"] else ("⚠️" if s["percent"] >= 75 else "✅")
            lines.append(
                f"{mark} <b>{sid}</b>: {s['percent']:.1f}% "
                f"({fmt_bytes(s['used_bytes'])} / {fmt_bytes(s['limit_bytes'])}) "
                f"— обновлено {age}с назад"
            )
        await tg.send_message(chat_id, "\n".join(lines))

    elif cmd == "/servers":
        if not state["servers"]:
            await tg.send_message(chat_id, "Серверов нет.")
            return
        sids = ", ".join(sorted(state["servers"].keys()))
        await tg.send_message(chat_id, f"Известные серверы: {sids}")

    elif cmd == "/help" or cmd == "/start":
        await tg.send_message(chat_id, (
            "<b>Команды:</b>\n"
            "/status — сводка по всем серверам\n"
            "/servers — список серверов\n"
            "/help — это сообщение"
        ))

    else:
        await tg.send_message(chat_id, f"Неизвестная команда: {cmd}. /help")


# ---------- фоновые задачи ----------

async def poll_telegram(cfg: Config, state: dict, tg: TelegramClient) -> None:
    while True:
        try:
            updates = await tg.get_updates()
            for upd in updates:
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                text = msg.get("text", "")
                chat_id = msg["chat"]["id"]
                if text.startswith("/"):
                    await handle_command(cfg, state, tg, chat_id, text)
        except Exception:
            log.exception("Ошибка опроса Telegram")
            await asyncio.sleep(5)


async def daily_report(cfg: Config, state: dict, tg: TelegramClient) -> None:
    import datetime as dt
    while True:
        now = dt.datetime.now()
        target = now.replace(
            hour=cfg.daily_report_hour, minute=0, second=0, microsecond=0,
        )
        if target <= now:
            target += dt.timedelta(days=1)
        sleep_for = (target - now).total_seconds()
        log.info("Ежедневный отчёт через %d секунд", int(sleep_for))
        await asyncio.sleep(sleep_for)

        if not state["servers"]:
            await tg.broadcast("📊 Ежедневный отчёт: данных нет.")
            continue
        lines = ["📊 <b>Ежедневный отчёт по трафику</b>"]
        for sid, s in sorted(state["servers"].items()):
            mark = "🛑" if s["blocked"] else ("⚠️" if s["percent"] >= 75 else "✅")
            lines.append(
                f"{mark} <b>{sid}</b>: {s['percent']:.1f}% "
                f"({fmt_bytes(s['used_bytes'])} / {fmt_bytes(s['limit_bytes'])})"
            )
        await tg.broadcast("\n".join(lines))


async def silence_watchdog(cfg: Config, state: dict, tg: TelegramClient) -> None:
    """Если агент молчит дольше agent_silence_alert — алертим."""
    while True:
        await asyncio.sleep(300)  # проверка раз в 5 минут
        now = time.time()
        for sid, s in state["servers"].items():
            silence = now - s["ts"]
            if silence > cfg.agent_silence_alert:
                last = state["last_silence_alert"].get(sid, 0)
                # повторно алертить не чаще раза в сутки
                if now - last > 86400:
                    await tg.broadcast(
                        f"🔕 <b>[{sid}]</b> молчит уже "
                        f"{int(silence / 60)} минут"
                    )
                    state["last_silence_alert"][sid] = now
                    save_state(state)


# ---------- запуск ----------

async def main() -> None:
    cfg = Config.load(CONFIG_PATH)
    state = load_state()

    async with ClientSession() as session:
        tg = TelegramClient(cfg, session)
        app = make_app(cfg, state, tg)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, cfg.listen_host, cfg.listen_port)
        await site.start()
        log.info("HTTP listen on %s:%s", cfg.listen_host, cfg.listen_port)

        await asyncio.gather(
            poll_telegram(cfg, state, tg),
            daily_report(cfg, state, tg),
            silence_watchdog(cfg, state, tg),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
