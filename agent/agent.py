#!/usr/bin/env python3
"""
Traffic monitor agent.

Запускается на каждом сервере. Считает суммарный трафик через vnstat,
шлёт отчёты центральному боту, при превышении лимита блокирует
трафик через ufw.

Зависимости:
    apt install vnstat python3-yaml python3-requests ufw
    systemctl enable --now vnstat
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

CONFIG_PATH = Path("/etc/traffic_monitor/agent.yaml")
STATE_PATH = Path("/var/lib/traffic_monitor/state.json")
LOG_PATH = Path("/var/log/traffic_monitor-agent.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("agent")


# ---------- конфиг и состояние ----------

@dataclass
class Config:
    server_id: str
    interface: str
    limit_bytes: int
    reset_day: int
    thresholds_percent: list[int]
    thresholds_remaining: list[int]
    auto_block: bool
    bot_url: str
    shared_secret: str
    report_interval: int
    check_interval: int

    @classmethod
    def load(cls, path: Path) -> "Config":
        with path.open() as f:
            d = yaml.safe_load(f)
        return cls(
            server_id=d["server_id"],
            interface=d["interface"],
            limit_bytes=int(d["limit_bytes"]),
            reset_day=int(d.get("reset_day", 1)),
            thresholds_percent=list(d["thresholds"]["percent"]),
            thresholds_remaining=list(d["thresholds"]["remaining_bytes"]),
            auto_block=bool(d.get("auto_block", True)),
            bot_url=d["bot_url"].rstrip("/"),
            shared_secret=d["shared_secret"],
            report_interval=int(d.get("report_interval", 3600)),
            check_interval=int(d.get("check_interval", 300)),
        )

    @property
    def bot_host(self) -> str:
        """Хост бота из bot_url, для allow-правила UFW."""
        p = urlparse(self.bot_url)
        return p.hostname or ""

    @property
    def bot_port(self) -> int:
        """Порт бота из bot_url. Если не указан — 80/443 по схеме."""
        p = urlparse(self.bot_url)
        if p.port:
            return p.port
        return 443 if p.scheme == "https" else 80


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            log.exception("Не удалось прочитать state, начинаю с чистого")
    return {
        "blocked": False,
        "fired_percent": [],      # какие % уже отправлены
        "fired_remaining": [],    # какие пороги "осталось N" уже отправлены
        "last_report_ts": 0,
        "unblock_until_reset": False,    # ручная разблокировка до следующего сброса
        "limit_override_bytes": None,    # ручной лимит на текущий период (используется вместо cfg.limit_bytes)
    }


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


# ---------- сбор трафика через vnstat ----------

def get_monthly_usage(interface: str) -> int:
    """
    Возвращает rx+tx за текущий месяц в байтах.
    Используем vnstat --json m — стабильный машиночитаемый формат.
    """
    result = subprocess.run(
        ["vnstat", "--json", "m", "-i", interface],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)

    # Структура: interfaces[0].traffic.month[] — список месяцев
    # Берём последний (текущий) месяц
    months = data["interfaces"][0]["traffic"]["month"]
    if not months:
        return 0
    current = months[-1]
    return int(current["rx"]) + int(current["tx"])


# ---------- блокировка через ufw ----------

def _ufw_bot_allow_args(bot_host: str, bot_port: int) -> list[str]:
    """Аргументы UFW для allow исходящих к боту."""
    return ["allow", "out", "to", bot_host,
            "port", str(bot_port), "proto", "tcp",
            "comment", "traffic-monitor bot"]


def ufw_allow_bot(bot_host: str, bot_port: int) -> None:
    """
    Разрешает исходящие соединения к боту. Должно вызываться ДО
    `default deny outgoing`, иначе агент не сможет рапортовать о блокировке
    и принимать команды (например, /unblock из Telegram).
    """
    if not bot_host:
        log.warning("bot_host пуст — не могу добавить allow-правило для бота")
        return
    cmd = ["ufw"] + _ufw_bot_allow_args(bot_host, bot_port)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        log.info("UFW: разрешено исходящее к боту %s:%s", bot_host, bot_port)
    except subprocess.CalledProcessError as e:
        # 'Skipping adding existing rule' — это нормально, не считаем ошибкой
        if "existing rule" in (e.stdout or "") + (e.stderr or ""):
            log.info("UFW: правило для бота %s:%s уже есть", bot_host, bot_port)
        else:
            log.warning("UFW: не удалось добавить allow для бота: %s",
                        e.stderr or e.stdout or e)


def ufw_remove_bot_allow(bot_host: str, bot_port: int) -> None:
    """
    Удаляет allow-правило для бота, добавленное в ufw_allow_bot.
    Безопасно вызывать, если правила нет — UFW просто ругнётся, мы это глотаем.
    """
    if not bot_host:
        return
    cmd = ["ufw", "delete"] + _ufw_bot_allow_args(bot_host, bot_port)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        log.info("UFW: удалено allow-правило для бота %s:%s", bot_host, bot_port)
    except subprocess.CalledProcessError as e:
        log.debug("UFW: не удалось удалить правило для бота (возможно его нет): %s",
                  e.stderr or e.stdout or e)


def ufw_block_all(bot_host: str = "", bot_port: int = 0) -> None:
    """
    Полная блокировка трафика: меняем дефолтные политики на deny,
    плюс явно отрубаем форвардинг. SSH-доступ к серверу сохраняется
    только если у вас уже есть allow-правило для SSH-порта.
    Если задан bot_host/bot_port — сначала добавляем allow для бота,
    чтобы агент мог продолжать слать отчёты и принимать команды.
    """
    if bot_host and bot_port:
        ufw_allow_bot(bot_host, bot_port)
    log.warning("БЛОКИРОВКА ТРАФИКА: ufw default deny incoming/outgoing")
    subprocess.run(["ufw", "--force", "default", "deny", "incoming"], check=True)
    subprocess.run(["ufw", "--force", "default", "deny", "outgoing"], check=True)
    subprocess.run(["ufw", "reload"], check=True)


def ufw_unblock(bot_host: str = "", bot_port: int = 0) -> None:
    """
    Возврат к нормальной работе: incoming deny, outgoing allow.
    Это стандартный UFW-дефолт. Заодно убираем bot-allow,
    оно избыточно при `default allow outgoing`.
    """
    log.info("СНЯТИЕ БЛОКИРОВКИ: ufw default deny incoming / allow outgoing")
    subprocess.run(["ufw", "--force", "default", "deny", "incoming"], check=True)
    subprocess.run(["ufw", "--force", "default", "allow", "outgoing"], check=True)
    subprocess.run(["ufw", "reload"], check=True)
    if bot_host and bot_port:
        ufw_remove_bot_allow(bot_host, bot_port)


# ---------- связь с ботом ----------

def send_to_bot(cfg: Config, endpoint: str, payload: dict) -> bool:
    url = f"{cfg.bot_url}{endpoint}"
    headers = {"X-Auth-Token": cfg.shared_secret}
    payload = dict(payload, server_id=cfg.server_id)
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        log.warning("Не удалось отправить боту %s: %s", endpoint, e)
        return False


def poll_commands(cfg: Config) -> list[str]:
    """
    Запрашивает у бота список ожидающих команд для этого server_id.
    Возвращает список команд (строк). Сам факт получения команды
    означает, что бот её из своей очереди удалил.
    """
    url = f"{cfg.bot_url}/commands"
    headers = {"X-Auth-Token": cfg.shared_secret}
    params = {"server_id": cfg.server_id}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        return list(data.get("commands", []))
    except requests.RequestException as e:
        log.debug("Не удалось получить команды от бота: %s", e)
        return []
    except (ValueError, KeyError) as e:
        log.warning("Некорректный ответ /commands: %s", e)
        return []


# ---------- проверка порогов ----------

def check_thresholds(cfg: Config, state: dict, used: int,
                     effective_limit: int) -> list[str]:
    """
    Возвращает список текстов уведомлений, которые надо отправить.
    Обновляет state, чтобы не слать одно и то же дважды.
    effective_limit — фактический лимит для расчёта процентов (может отличаться
    от cfg.limit_bytes, если был override через /unblock).
    """
    messages: list[str] = []
    percent = (used / effective_limit) * 100
    remaining = effective_limit - used

    # Триггеры по проценту
    for p in sorted(cfg.thresholds_percent):
        if percent >= p and p not in state["fired_percent"]:
            messages.append(
                f"⚠️ Использовано {percent:.1f}% лимита "
                f"({fmt_bytes(used)} из {fmt_bytes(effective_limit)})"
            )
            state["fired_percent"].append(p)

    # Триггеры "осталось меньше N"
    for r in sorted(cfg.thresholds_remaining, reverse=True):
        if remaining <= r and r not in state["fired_remaining"]:
            messages.append(
                f"⚠️ Осталось менее {fmt_bytes(r)} трафика "
                f"(использовано {fmt_bytes(used)} из {fmt_bytes(effective_limit)})"
            )
            state["fired_remaining"].append(r)

    return messages


def fmt_bytes(n: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.2f} {u}"
        f /= 1024
    return f"{n} B"


# ---------- обработка команд от бота ----------

def handle_command(cfg: Config, state: dict, cmd: str) -> None:
    """
    Обрабатывает команду от бота. Поддерживается:
      - "unblock_grace:<bytes>" — снять блокировку, поставить новый лимит =
        текущее_использование + <bytes>. Override живёт до сброса периода.
    """
    if cmd.startswith("unblock_grace:"):
        try:
            grace_bytes = int(cmd.split(":", 1)[1])
        except (ValueError, IndexError):
            log.warning("Не могу распарсить команду: %r", cmd)
            return
        if grace_bytes <= 0:
            log.warning("unblock_grace с неположительным значением: %s", grace_bytes)
            return

        log.warning("Получена команда unblock_grace на %s байт", grace_bytes)

        # Смотрим свежий used, а не из state (между отчётами могло утечь много).
        try:
            current_used = get_monthly_usage(cfg.interface)
        except Exception:
            log.exception("Не удалось получить текущий used — отменяю команду")
            send_to_bot(cfg, "/notify", {
                "text": "❌ Не удалось получить текущий трафик — команда отменена",
                "level": "critical",
            })
            return

        new_limit = current_used + grace_bytes
        state["limit_override_bytes"] = new_limit
        # Не используем unblock_until_reset: при превышении НОВОГО лимита
        # хотим, чтобы блокировка опять сработала. Поэтому только override.
        state["unblock_until_reset"] = False
        # Сбрасываем флаги порогов — для нового лимита они должны пересчитаться
        state["fired_percent"] = []
        state["fired_remaining"] = []

        # Снимаем UFW-блокировку
        if state.get("blocked"):
            try:
                ufw_unblock(cfg.bot_host, cfg.bot_port)
                state["blocked"] = False
            except Exception:
                log.exception("Не удалось снять UFW-блокировку")
                send_to_bot(cfg, "/notify", {
                    "text": "❌ Не удалось снять UFW-блокировку — см. логи агента",
                    "level": "critical",
                })
                return

        send_to_bot(cfg, "/notify", {
            "text": (
                f"✅ Блокировка снята. Новый лимит до конца расчётного периода: "
                f"{fmt_bytes(new_limit)} "
                f"(использовано {fmt_bytes(current_used)} + запас {fmt_bytes(grace_bytes)}). "
                f"При превышении нового лимита блокировка сработает снова."
            ),
            "level": "info",
        })
    else:
        log.warning("Неизвестная команда от бота: %r", cmd)


# ---------- сброс счётчика при новом периоде ----------

def maybe_reset_period(cfg: Config, state: dict) -> None:
    """
    Если наступил новый расчётный период — обнуляем флаги
    срабатывания порогов и снимаем блокировку.
    """
    import datetime as dt
    today = dt.date.today()
    last = state.get("period_start")
    # вычисляем начало текущего периода
    if today.day >= cfg.reset_day:
        period_start = today.replace(day=cfg.reset_day).isoformat()
    else:
        prev = today.replace(day=1) - dt.timedelta(days=1)
        period_start = prev.replace(day=cfg.reset_day).isoformat()

    if last != period_start:
        log.info("Новый расчётный период (%s), сбрасываю состояние", period_start)
        state["fired_percent"] = []
        state["fired_remaining"] = []
        state["period_start"] = period_start
        state["unblock_until_reset"] = False  # ручная разблокировка действует только в рамках периода
        state["limit_override_bytes"] = None  # override лимита тоже только на период
        if state.get("blocked"):
            ufw_unblock(cfg.bot_host, cfg.bot_port)
            state["blocked"] = False


# ---------- основной цикл ----------

def main() -> int:
    if os.geteuid() != 0:
        log.error("Агент должен запускаться от root (нужен доступ к ufw)")
        return 1

    cfg = Config.load(CONFIG_PATH)
    state = load_state()
    log.info("Старт: server_id=%s, лимит=%s", cfg.server_id, fmt_bytes(cfg.limit_bytes))

    while True:
        try:
            maybe_reset_period(cfg, state)
            used = get_monthly_usage(cfg.interface)

            # Эффективный лимит: если был override от /unblock — используем его,
            # иначе исходный из конфига. Override живёт до сброса периода.
            effective_limit = state.get("limit_override_bytes") or cfg.limit_bytes
            percent = (used / effective_limit) * 100
            log.info("Использовано: %s (%.2f%% от %s%s)",
                     fmt_bytes(used), percent, fmt_bytes(effective_limit),
                     " [override]" if state.get("limit_override_bytes") else "")

            # Уведомления по порогам — относительно эффективного лимита
            messages = check_thresholds(cfg, state, used, effective_limit)
            for msg in messages:
                send_to_bot(cfg, "/notify", {"text": msg, "level": "warning"})

            # Блокировка при превышении.
            # Пропускаем, если в этом периоде уже была ручная разблокировка
            # БЕЗ нового лимита (unblock_until_reset) — иначе агент сразу
            # бы заблокировал снова. С override-лимитом блокировка работает
            # как обычно, но относительно нового лимита.
            manual_override = state.get("unblock_until_reset", False)
            if used >= effective_limit and not state["blocked"] and not manual_override:
                if cfg.auto_block:
                    ufw_block_all(cfg.bot_host, cfg.bot_port)
                    state["blocked"] = True
                    send_to_bot(cfg, "/notify", {
                        "text": f"🛑 ЛИМИТ ПРЕВЫШЕН! Трафик заблокирован "
                                f"(использовано {fmt_bytes(used)} из "
                                f"{fmt_bytes(effective_limit)})",
                        "level": "critical",
                    })
                else:
                    send_to_bot(cfg, "/notify", {
                        "text": f"🛑 Лимит превышен (auto_block=false)",
                        "level": "critical",
                    })

            # Опрос команд от бота (/unblock и т.п.)
            for cmd in poll_commands(cfg):
                handle_command(cfg, state, cmd)

            # Периодический отчёт боту
            now = time.time()
            if now - state["last_report_ts"] >= cfg.report_interval:
                send_to_bot(cfg, "/report", {
                    "used_bytes": used,
                    "limit_bytes": effective_limit,
                    "original_limit_bytes": cfg.limit_bytes,
                    "percent": round(percent, 2),
                    "blocked": state["blocked"],
                })
                state["last_report_ts"] = now

            save_state(state)
        except Exception:
            log.exception("Ошибка в основном цикле")

        time.sleep(cfg.check_interval)


if __name__ == "__main__":
    sys.exit(main())
