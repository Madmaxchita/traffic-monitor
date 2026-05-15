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

def ufw_block_all() -> None:
    """
    Полная блокировка трафика: меняем дефолтные политики на deny,
    плюс явно отрубаем форвардинг. SSH-доступ к серверу сохраняется
    только если у вас уже есть allow-правило для SSH-порта.
    """
    log.warning("БЛОКИРОВКА ТРАФИКА: ufw default deny incoming/outgoing")
    subprocess.run(["ufw", "--force", "default", "deny", "incoming"], check=True)
    subprocess.run(["ufw", "--force", "default", "deny", "outgoing"], check=True)
    subprocess.run(["ufw", "reload"], check=True)


def ufw_unblock() -> None:
    """
    Возврат к нормальной работе: incoming deny, outgoing allow.
    Это стандартный UFW-дефолт.
    """
    log.info("СНЯТИЕ БЛОКИРОВКИ: ufw default deny incoming / allow outgoing")
    subprocess.run(["ufw", "--force", "default", "deny", "incoming"], check=True)
    subprocess.run(["ufw", "--force", "default", "allow", "outgoing"], check=True)
    subprocess.run(["ufw", "reload"], check=True)


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


# ---------- проверка порогов ----------

def check_thresholds(cfg: Config, state: dict, used: int) -> list[str]:
    """
    Возвращает список текстов уведомлений, которые надо отправить.
    Обновляет state, чтобы не слать одно и то же дважды.
    """
    messages: list[str] = []
    percent = (used / cfg.limit_bytes) * 100
    remaining = cfg.limit_bytes - used

    # Триггеры по проценту
    for p in sorted(cfg.thresholds_percent):
        if percent >= p and p not in state["fired_percent"]:
            messages.append(
                f"⚠️ Использовано {percent:.1f}% лимита "
                f"({fmt_bytes(used)} из {fmt_bytes(cfg.limit_bytes)})"
            )
            state["fired_percent"].append(p)

    # Триггеры "осталось меньше N"
    for r in sorted(cfg.thresholds_remaining, reverse=True):
        if remaining <= r and r not in state["fired_remaining"]:
            messages.append(
                f"⚠️ Осталось менее {fmt_bytes(r)} трафика "
                f"(использовано {fmt_bytes(used)} из {fmt_bytes(cfg.limit_bytes)})"
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
        if state.get("blocked"):
            ufw_unblock()
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
            percent = (used / cfg.limit_bytes) * 100
            log.info("Использовано: %s (%.2f%%)", fmt_bytes(used), percent)

            # Уведомления по порогам
            messages = check_thresholds(cfg, state, used)
            for msg in messages:
                send_to_bot(cfg, "/notify", {"text": msg, "level": "warning"})

            # Блокировка при превышении
            if used >= cfg.limit_bytes and not state["blocked"]:
                if cfg.auto_block:
                    ufw_block_all()
                    state["blocked"] = True
                    send_to_bot(cfg, "/notify", {
                        "text": f"🛑 ЛИМИТ ПРЕВЫШЕН! Трафик заблокирован "
                                f"(использовано {fmt_bytes(used)})",
                        "level": "critical",
                    })
                else:
                    send_to_bot(cfg, "/notify", {
                        "text": f"🛑 Лимит превышен (auto_block=false)",
                        "level": "critical",
                    })

            # Периодический отчёт боту
            now = time.time()
            if now - state["last_report_ts"] >= cfg.report_interval:
                send_to_bot(cfg, "/report", {
                    "used_bytes": used,
                    "limit_bytes": cfg.limit_bytes,
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
