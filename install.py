#!/usr/bin/env python3
"""
Traffic Monitor — красивый интерактивный установщик.

Использование:
    sudo python3 install.py            # интерактивное меню
    sudo python3 install.py agent      # сразу агента
    sudo python3 install.py bot        # сразу бота
    sudo python3 install.py both       # бота + агента
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# ---------- авто-установка rich ----------
def ensure_rich():
    try:
        import rich  # noqa: F401
        return
    except ImportError:
        pass
    print("📦 Устанавливаю библиотеку rich для красивого интерфейса...")
    cmds = [
        ["apt", "install", "-y", "-qq", "python3-rich"],
        ["pip3", "install", "--break-system-packages", "rich"],
        ["pip3", "install", "rich"],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            print("✓ rich установлен")
            return
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    print("✗ Не удалось установить rich. Поставьте вручную: pip3 install rich")
    sys.exit(1)
    sys.exit(1)


ensure_rich()

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm as _RichConfirm, IntPrompt
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.live import Live
from rich.text import Text
from rich.align import Align
from rich import box
from rich.theme import Theme
from rich.markdown import Markdown

# ---------- настройки ----------
REPO_URL = "https://github.com/Madmaxchita/traffic-monitor.git"
REPO_DIR = Path("/opt/traffic-monitor")
CONFIG_DIR = Path("/etc/traffic_monitor")
STATE_DIR = Path("/var/lib/traffic_monitor")
BIN_DIR = Path("/usr/local/bin")
SYSTEMD_DIR = Path("/etc/systemd/system")

# ---------- консоль с темой ----------
theme = Theme({
    "info":    "cyan",
    "ok":      "bold green",
    "warn":    "bold yellow",
    "err":     "bold red",
    "prompt":  "bold magenta",
    "muted":   "dim white",
    "title":   "bold white on blue",
    "accent":  "bold bright_magenta",
})
console = Console(theme=theme, highlight=False)


# ============================================================
#  Вспомогательные функции
# ============================================================

def banner():
    """Большой заголовок при запуске."""
    art = """
[bold bright_cyan]  ╔══════════════════════════════════════════════════════╗
  ║   📡  T R A F F I C   M O N I T O R   📡              ║
  ║   Установщик для бота и агентов мониторинга трафика   ║
  ╚══════════════════════════════════════════════════════╝[/bold bright_cyan]
    """
    console.print(art)


def step(msg: str) -> None:
    console.print(f"[info]➤[/info]  {msg}")


def ok(msg: str) -> None:
    console.print(f"[ok]✓[/ok]  {msg}")


def warn(msg: str) -> None:
    console.print(f"[warn]⚠[/warn]  {msg}")


def err(msg: str) -> None:
    console.print(f"[err]✗[/err]  {msg}")


def die(msg: str) -> None:
    err(msg)
    sys.exit(1)


def require_root():
    if os.geteuid() != 0:
        die("Запускайте от root: sudo python3 install.py")


class Confirm:
    """
    Обёртка над rich.Confirm с поддержкой русского ввода (д/н, да/нет)
    и устойчивостью к произвольному мусору в ответе.
    """
    @staticmethod
    def ask(prompt: str, default: bool = False) -> bool:
        yes_tokens = {"y", "yes", "д", "да", "1", "true", "т"}
        no_tokens  = {"n", "no",  "н", "нет", "0", "false", "f"}
        suffix = " \\[Y/n] " if default else " \\[y/N] "
        for _ in range(10):
            try:
                ans = Prompt.ask(prompt + suffix, default="", show_default=False)
            except (EOFError, KeyboardInterrupt):
                raise
            ans = ans.strip().lower()
            if ans == "":
                return default
            if ans in yes_tokens:
                return True
            if ans in no_tokens:
                return False
            warn(f"Не понял ответ '[muted]{ans}[/muted]'. Введите y/n, д/н или Enter для значения по умолчанию.")
        die("Слишком много неверных ответов")


def run(cmd: list[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Запустить команду; capture=True — поймать stdout/stderr."""
    return subprocess.run(
        cmd, check=check,
        capture_output=capture,
        text=True,
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
    )


def run_quiet(cmd: list[str]) -> bool:
    """Запустить тихо, вернуть True/False."""
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def systemd_is_active(svc: str) -> bool:
    res = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True)
    return res.stdout.strip() == "active"


def is_installed(component: str) -> bool:
    return (SYSTEMD_DIR / f"traffic-{component}.service").exists()


# ============================================================
#  Системные операции с красивым прогрессом
# ============================================================

def install_packages(packages: list[str]) -> None:
    step(f"Устанавливаю пакеты: [accent]{', '.join(packages)}[/accent]")
    with Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console, transient=True,
    ) as progress:
        progress.add_task("apt update", total=None)
        run(["apt", "update", "-qq"], capture=True)
        progress.add_task(f"apt install {' '.join(packages)}", total=None)
        run(["apt", "install", "-y", "-qq"] + packages, capture=True)
    ok("Пакеты установлены")


def ensure_repo() -> None:
    if (REPO_DIR / ".git").exists():
        step(f"Обновляю репозиторий [muted]{REPO_DIR}[/muted]")
        with console.status("[cyan]git pull...", spinner="dots"):
            run_quiet(["git", "-C", str(REPO_DIR), "pull", "--quiet"])
    else:
        step(f"Клоню [muted]{REPO_URL}[/muted]")
        if REPO_DIR.exists():
            run(["rm", "-rf", str(REPO_DIR)])
        with console.status("[cyan]git clone...", spinner="dots"):
            run(["git", "clone", "--quiet", REPO_URL, str(REPO_DIR)])
    ok("Репозиторий готов")


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def detect_iface() -> str:
    """Интерфейс по умолчанию (через который маршрут наружу)."""
    try:
        out = subprocess.check_output(["ip", "-4", "route", "show", "default"], text=True)
        m = re.search(r"dev\s+(\S+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "eth0"


def gen_secret() -> str:
    return secrets.token_hex(32)


def telegram_get_me(api_base: str, token: str) -> dict | None:
    url = f"{api_base.rstrip('/')}/bot{token}/getMe"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def http_probe(url: str) -> int:
    """Возвращает HTTP-код ответа, 0 при недоступности."""
    try:
        req = urllib.request.Request(url, method="POST",
                                     headers={"X-Auth-Token": "WRONG"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


# ============================================================
#  UFW — управление файрволом
# ============================================================

def ufw_installed() -> bool:
    return run_quiet(["which", "ufw"])


def ufw_active() -> bool:
    """True если UFW в режиме active."""
    if not ufw_installed():
        return False
    try:
        out = subprocess.check_output(["ufw", "status"], text=True, stderr=subprocess.DEVNULL)
        return "Status: active" in out
    except Exception:
        return False


def ufw_status_text() -> str:
    try:
        return subprocess.check_output(["ufw", "status", "verbose"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return "(ufw недоступен)"


def ufw_has_ssh_rule() -> bool:
    """Проверяет, есть ли в UFW правило для SSH-порта.
    Берёт текущий порт sshd из его конфига, плюс на всякий случай порт текущей SSH-сессии."""
    try:
        out = subprocess.check_output(["ufw", "status"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return False

    ssh_ports = set()
    # из sshd_config
    sshd = Path("/etc/ssh/sshd_config")
    if sshd.exists():
        for line in sshd.read_text().splitlines():
            line = line.strip()
            if line.lower().startswith("port "):
                try:
                    ssh_ports.add(int(line.split()[1]))
                except Exception:
                    pass
    # из подкаталога sshd_config.d
    sshd_d = Path("/etc/ssh/sshd_config.d")
    if sshd_d.is_dir():
        for f in sshd_d.glob("*.conf"):
            for line in f.read_text().splitlines():
                line = line.strip()
                if line.lower().startswith("port "):
                    try:
                        ssh_ports.add(int(line.split()[1]))
                    except Exception:
                        pass
    # если нигде явно не указан — значит дефолтный 22
    if not ssh_ports:
        ssh_ports.add(22)

    # из активной SSH-сессии (запасной вариант)
    sock = os.environ.get("SSH_CLIENT") or os.environ.get("SSH_CONNECTION")
    if sock:
        parts = sock.split()
        if len(parts) >= 3 and parts[-1].isdigit():
            ssh_ports.add(int(parts[-1]))

    # ищем эти порты в выводе ufw
    text = out.lower()
    for port in ssh_ports:
        if re.search(rf"\b{port}/tcp\b", text) or re.search(rf"\b{port}\b", text):
            return True
    # ещё могут быть алиасы 'ssh' или 'OpenSSH'
    if re.search(r"\bopenssh\b|\bssh\b", text):
        return True
    return False


def ufw_open_port(port: int, sources: list[str] | None = None,
                  comment: str = "") -> tuple[bool, str]:
    """
    Открыть порт в UFW. sources=None → разрешить всем.
    sources=["1.2.3.4", "5.6.7.8/24"] → разрешить только этим.
    Возвращает (success, log_text).
    """
    log_lines = []
    if not sources:
        cmd = ["ufw", "allow", f"{port}/tcp"]
        if comment:
            cmd += ["comment", comment]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, check=True)
            log_lines.append(r.stdout.strip())
            return True, "\n".join(log_lines)
        except subprocess.CalledProcessError as e:
            return False, e.stderr or str(e)
    else:
        ok_any = False
        for src in sources:
            cmd = ["ufw", "allow", "from", src, "to", "any",
                   "port", str(port), "proto", "tcp"]
            if comment:
                cmd += ["comment", f"{comment} {src}"]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, check=True)
                log_lines.append(f"  ✓ {src}: {r.stdout.strip()}")
                ok_any = True
            except subprocess.CalledProcessError as e:
                log_lines.append(f"  ✗ {src}: {e.stderr.strip()}")
        return ok_any, "\n".join(log_lines)


def validate_ip_or_cidr(s: str) -> bool:
    """Проверяет, что строка похожа на IPv4 или IPv4/CIDR."""
    s = s.strip()
    m = re.match(r"^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(/\d{1,2})?$", s)
    if not m:
        return False
    octets = m.group(1).split(".")
    if not all(0 <= int(o) <= 255 for o in octets):
        return False
    if m.group(2):
        mask = int(m.group(2)[1:])
        if not 0 <= mask <= 32:
            return False
    return True


# ============================================================
#  Сбор параметров — АГЕНТ
# ============================================================

def collect_agent_params() -> dict:
    console.print()
    console.print(Panel(
        "[bold]Установка АГЕНТА[/bold]\n\n"
        "Агент считает трафик через [accent]vnstat[/accent], отчитывается боту "
        "и при превышении лимита блокирует трафик через [accent]ufw[/accent].",
        title="🤖 Агент", border_style="cyan", expand=False,
    ))
    console.print()

    default_id = os.uname().nodename.split(".")[0]
    server_id = Prompt.ask("🏷  [prompt]Идентификатор сервера[/prompt]",
                           default=default_id)

    default_iface = detect_iface()
    iface = Prompt.ask("🌐 [prompt]Сетевой интерфейс[/prompt]",
                       default=default_iface)

    # Проверим существование
    if not run_quiet(["ip", "link", "show", iface]):
        avail = subprocess.check_output(["ip", "-br", "link"], text=True)
        warn(f"Интерфейс [accent]{iface}[/accent] не найден")
        console.print(Panel(avail, title="Доступные интерфейсы", border_style="yellow"))
        if not Confirm.ask("Продолжить?", default=False):
            return collect_agent_params()

    console.print("\n💾 [prompt]Лимит трафика[/prompt]")
    table = Table(box=box.SIMPLE, show_header=False)
    table.add_column("Опция", style="accent")
    table.add_column("Описание")
    table.add_column("Байт", style="muted")
    table.add_row("1", "30 TiB — хостинг считает в TiB (2⁴⁰)", "32 985 348 833 280")
    table.add_row("2", "30 TB  — хостинг считает в TB  (10¹²)", "30 000 000 000 000")
    table.add_row("3", "Своё значение", "—")
    console.print(table)
    limit_choice = Prompt.ask("Выбор", choices=["1", "2", "3"], default="1")
    if limit_choice == "1":
        limit_bytes = 32985348833280
    elif limit_choice == "2":
        limit_bytes = 30000000000000
    else:
        limit_bytes = IntPrompt.ask("Лимит в байтах", default=32985348833280)

    reset_day = IntPrompt.ask("📅 [prompt]День сброса счётчика[/prompt] (1-28)", default=1)

    # Bot URL — собираем по частям: хост (IP/домен), порт, схема (по умолчанию http)
    console.print()
    console.print("🔗 [prompt]Адрес центрального бота[/prompt]")
    bot_host = Prompt.ask("   IP или домен (без http:// и без порта)",
                          default="127.0.0.1")
    bot_host = bot_host.strip().rstrip("/")
    # На случай если пользователь всё же ввёл http:// — уберём его
    bot_host = re.sub(r"^https?://", "", bot_host)
    # И на случай если ввёл с портом — отделим
    if ":" in bot_host:
        bot_host, maybe_port = bot_host.rsplit(":", 1)
        if maybe_port.isdigit():
            warn(f"Порт [accent]{maybe_port}[/accent] взят из адреса")
            bot_port_default = int(maybe_port)
        else:
            bot_port_default = 8080
    else:
        bot_port_default = 8080

    bot_port = IntPrompt.ask("   Порт", default=bot_port_default)

    use_https = False
    if Confirm.ask("   Использовать [accent]HTTPS[/accent]?", default=False):
        use_https = True

    scheme = "https" if use_https else "http"
    bot_url = f"{scheme}://{bot_host}:{bot_port}"
    ok(f"Адрес бота: [accent]{bot_url}[/accent]")

    console.print()
    console.print(Panel(
        "[bold]Shared secret — общий ключ между ботом и всеми агентами.[/bold]\n\n"
        "Где взять:\n"
        "  • Если бот уже установлен — секрет лежит в [accent]/etc/traffic_monitor/bot.yaml[/accent]\n"
        "    на сервере с ботом, в поле [accent]shared_secret[/accent]\n"
        "  • Посмотреть командой на сервере-боте:\n"
        "    [accent]grep shared_secret /etc/traffic_monitor/bot.yaml[/accent]\n"
        "  • Или его показал установщик бота при первой установке (если генерировал автоматически)\n\n"
        "[warn]Должен совпадать буква-в-букву с тем, что в боте.[/warn]",
        title="🔑 Где взять shared_secret", border_style="cyan",
    ))
    secret = Prompt.ask("🔑 [prompt]Shared secret[/prompt]", password=True)
    if not secret:
        die("Секрет не может быть пустым")

    auto_block = False
    console.print()
    if Confirm.ask("🛑 Включить [warn]auto-block[/warn] (блокировка трафика при превышении)?",
                   default=False):
        console.print(Panel(
            "[warn]ВАЖНО:[/warn] перед включением убедитесь, что в UFW есть явное "
            "[accent]allow[/accent] правило для вашего SSH-порта.\n\n"
            "Иначе при срабатывании блокировки вы [err]потеряете SSH-доступ[/err] "
            "к серверу!",
            title="⚠️  Внимание", border_style="yellow",
        ))
        if Confirm.ask("В UFW есть allow-правило для SSH?", default=False):
            auto_block = True
        else:
            warn("Оставляю auto_block=false. Включите вручную позже в /etc/traffic_monitor/agent.yaml")

    # ---------- сводка ----------
    console.print()
    summary = Table(title="📋 Параметры", box=box.ROUNDED, show_header=False)
    summary.add_column("", style="info")
    summary.add_column("", style="accent")
    summary.add_row("🏷  Server ID",   server_id)
    summary.add_row("🌐 Интерфейс",    iface)
    summary.add_row("💾 Лимит",        f"{limit_bytes:,} байт ({limit_bytes / 1024**4:.1f} TiB)")
    summary.add_row("📅 День сброса",  str(reset_day))
    summary.add_row("🔗 Bot URL",      bot_url)
    summary.add_row("🛑 Auto-block",   "✅ да" if auto_block else "❌ нет")
    summary.add_row("🔑 Secret",       "[muted](скрыт)[/muted]")
    console.print(summary)
    console.print()

    if not Confirm.ask("[prompt]Начать установку?[/prompt]", default=True):
        die("Установка отменена")

    return dict(
        server_id=server_id, interface=iface, limit_bytes=limit_bytes,
        reset_day=reset_day, bot_url=bot_url, shared_secret=secret,
        auto_block=auto_block,
    )


# ============================================================
#  Сбор параметров — БОТ
# ============================================================

def collect_bot_params() -> dict:
    console.print()
    console.print(Panel(
        "[bold]Установка БОТА[/bold]\n\n"
        "Бот принимает HTTP-отчёты от агентов и шлёт уведомления в "
        "[accent]Telegram[/accent] через self-host API.",
        title="🤖 Бот", border_style="cyan", expand=False,
    ))
    console.print()

    token = Prompt.ask("🔑 [prompt]Telegram Bot Token[/prompt] (от @BotFather)",
                       password=True)
    if not token or ":" not in token:
        warn("Токен выглядит необычно (формат: 123456:AAH...)")

    api_base = Prompt.ask("🌐 [prompt]URL self-host Telegram Bot API[/prompt]")
    api_base = api_base.rstrip("/")
    if not api_base.startswith(("http://", "https://")):
        die("URL должен начинаться с http:// или https://")

    # Проверка токена
    step("Проверяю Telegram API...")
    with console.status("[cyan]getMe...", spinner="dots"):
        resp = telegram_get_me(api_base, token)

    if resp and resp.get("ok"):
        bot_user = resp.get("result", {}).get("username", "?")
        ok(f"API подтвердил токен. Бот: [accent]@{bot_user}[/accent]")
    else:
        err_msg = resp.get("error") or resp.get("description") or "неизвестная ошибка"
        warn(f"API не подтвердил токен: {err_msg}")
        if not Confirm.ask("Продолжить всё равно?", default=False):
            die("Установка отменена")

    # chat_ids
    console.print()
    console.print("💬 [prompt]Telegram chat_id для уведомлений[/prompt]")
    console.print("[muted]   Узнать chat_id: напишите боту любое сообщение, потом[/muted]")
    console.print(f"[muted]   curl '{api_base}/bot<TOKEN>/getUpdates' | python3 -m json.tool[/muted]")
    chat_ids = []
    while True:
        cid = Prompt.ask(
            f"   Chat ID #{len(chat_ids) + 1} [muted](Enter — закончить)[/muted]",
            default="", show_default=False,
        )
        if not cid:
            break
        if not re.match(r"^-?\d+$", cid):
            warn("Должно быть число")
            continue
        chat_ids.append(int(cid))
        ok(f"Добавлен chat_id [accent]{cid}[/accent]")

    if not chat_ids:
        die("Нужен хотя бы один chat_id")

    # listen
    console.print()
    console.print("📡 [prompt]На каком интерфейсе слушать?[/prompt]")
    table = Table(box=box.SIMPLE, show_header=False)
    table.add_column("", style="accent")
    table.add_column("Описание")
    table.add_row("1", "0.0.0.0 — все интерфейсы (для удалённых агентов)")
    table.add_row("2", "127.0.0.1 — только локально (бот+агент на одной машине)")
    console.print(table)
    listen_choice = Prompt.ask("Выбор", choices=["1", "2"], default="1")
    listen_host = "0.0.0.0" if listen_choice == "1" else "127.0.0.1"

    listen_port = IntPrompt.ask("🔌 [prompt]Порт[/prompt]", default=8080)

    # secret
    console.print()
    if Confirm.ask("🎲 Сгенерировать [prompt]shared_secret[/prompt] автоматически?",
                   default=True):
        secret = gen_secret()
        console.print()
        console.print(Panel(
            f"[accent]{secret}[/accent]\n\n"
            "[warn]СОХРАНИТЕ ЭТОТ СЕКРЕТ[/warn] — он нужен для всех агентов.",
            title="🔑 Shared Secret", border_style="bright_yellow",
        ))
        Prompt.ask("[muted]Нажмите Enter, когда сохранили[/muted]", default="")
    else:
        secret = Prompt.ask("🔑 [prompt]Shared secret[/prompt]", password=True)
        if not secret:
            die("Секрет не может быть пустым")

    daily_hour = IntPrompt.ask("⏰ [prompt]Час ежедневного отчёта[/prompt] (0-23)",
                               default=10)

    # ---------- сводка ----------
    console.print()
    summary = Table(title="📋 Параметры", box=box.ROUNDED, show_header=False)
    summary.add_column("", style="info")
    summary.add_column("", style="accent")
    summary.add_row("🌐 Telegram API", api_base)
    summary.add_row("💬 Chat IDs",     ", ".join(str(c) for c in chat_ids))
    summary.add_row("📡 Listen",       f"{listen_host}:{listen_port}")
    summary.add_row("⏰ Daily report", f"{daily_hour}:00")
    summary.add_row("🔑 Token",        "[muted](скрыт)[/muted]")
    summary.add_row("🔐 Secret",       "[muted](скрыт)[/muted]")
    console.print(summary)
    console.print()

    if not Confirm.ask("[prompt]Начать установку?[/prompt]", default=True):
        die("Установка отменена")

    return dict(
        telegram_token=token, telegram_api_base=api_base, chat_ids=chat_ids,
        listen_host=listen_host, listen_port=listen_port,
        shared_secret=secret, daily_report_hour=daily_hour,
    )


# ============================================================
#  Установка — АГЕНТ
# ============================================================

def write_agent_config(p: dict) -> None:
    cfg = CONFIG_DIR / "agent.yaml"
    cfg.write_text(f"""# Сгенерировано install.py {datetime.now().isoformat()}
server_id: "{p['server_id']}"
interface: "{p['interface']}"
limit_bytes: {p['limit_bytes']}
reset_day: {p['reset_day']}

thresholds:
  percent: [50, 75, 90]
  remaining_bytes: [5497558138880]   # 5 TiB

auto_block: {"true" if p['auto_block'] else "false"}

bot_url: "{p['bot_url']}"
shared_secret: "{p['shared_secret']}"
report_interval: 3600
check_interval: 300
""")
    cfg.chmod(0o600)
    ok(f"Конфиг создан: [muted]{cfg}[/muted]")


def install_agent_flow() -> None:
    if is_installed("agent"):
        warn("Агент уже установлен")
        if not Confirm.ask("Переустановить (с перезаписью конфига)?", default=False):
            return

    params = collect_agent_params()

    console.print()
    console.rule("[title] Установка [/title]", style="cyan")

    install_packages(["vnstat", "python3", "python3-yaml", "python3-requests", "ufw", "git", "openssl"])
    run_quiet(["systemctl", "enable", "--now", "vnstat"])

    ensure_repo()
    ensure_dirs()
    write_agent_config(params)

    step("Копирую файлы")
    run(["install", "-m", "0755", str(REPO_DIR / "agent/agent.py"),
         str(BIN_DIR / "traffic-agent")])
    run(["install", "-m", "0644", str(REPO_DIR / "agent/traffic-agent.service"),
         str(SYSTEMD_DIR / "traffic-agent.service")])
    run(["systemctl", "daemon-reload"])
    ok("Файлы установлены")

    # Проверка SSH-правила перед запуском (особенно важна при auto_block)
    check_ssh_rule_for_agent(params)

    step("Запускаю traffic-agent")
    run(["systemctl", "enable", "--now", "traffic-agent"], capture=True)
    time.sleep(3)

    console.print()
    verify_agent(params)


def check_ssh_rule_for_agent(params: dict) -> None:
    """Проверяет наличие SSH allow-правила; при auto_block — обязательно."""
    if not ufw_installed():
        return
    if not ufw_active():
        if params.get("auto_block"):
            console.print(Panel(
                "[warn]UFW не активен[/warn], но включена [accent]auto_block[/accent].\n\n"
                "Это означает, что при превышении лимита агент попытается включить\n"
                "[accent]default deny outgoing[/accent], но без активного UFW это не сработает.\n\n"
                "Включите UFW заранее (с обязательным allow для SSH-порта):\n"
                "  [accent]ufw allow OpenSSH[/accent]\n"
                "  [accent]ufw enable[/accent]",
                title="⚠️  UFW неактивен", border_style="yellow",
            ))
        return

    if ufw_has_ssh_rule():
        ok("В UFW найдено правило для SSH — auto_block безопасен")
        return

    # UFW активен, но SSH-правила нет — это критично!
    console.print(Panel(
        "[err]ВНИМАНИЕ: в UFW не найдено allow-правило для SSH-порта![/err]\n\n"
        "Если включена [accent]auto_block[/accent] и сработает блокировка трафика —\n"
        "вы можете [err]потерять SSH-доступ к серверу[/err].\n\n"
        "Текущий статус UFW:",
        title="🚨 КРИТИЧНО", border_style="red",
    ))
    console.print(Panel(ufw_status_text(), border_style="red"))

    if params.get("auto_block"):
        warn("Auto_block включён, но SSH-правила нет. Это опасно.")
        if Confirm.ask("Добавить allow-правило для SSH сейчас (откроет порт 22)?",
                       default=True):
            try:
                run(["ufw", "allow", "OpenSSH"], capture=True)
                ok("Правило OpenSSH добавлено")
            except Exception:
                # Fallback: явный порт 22
                run(["ufw", "allow", "22/tcp"], capture=True)
                ok("Правило для порта 22/tcp добавлено")
        else:
            if Confirm.ask("Отключить auto_block? (тогда блокировки не будет, "
                           "но и риска потерять SSH тоже нет)", default=True):
                # Перезаписываем конфиг с auto_block=false
                params["auto_block"] = False
                cfg_path = CONFIG_DIR / "agent.yaml"
                content = cfg_path.read_text()
                content = re.sub(r"^auto_block:.*$", "auto_block: false",
                                 content, flags=re.MULTILINE)
                cfg_path.write_text(content)
                ok("auto_block отключён в конфиге")
            else:
                warn("Оставляю как есть — будьте внимательны!")


def verify_agent(params: dict) -> None:
    console.rule("[title] Проверка [/title]", style="cyan")

    results = []

    # vnstat
    if systemd_is_active("vnstat"):
        results.append(("vnstat", True, "запущен"))
    else:
        results.append(("vnstat", False, "не запущен"))

    # YAML
    try:
        import yaml
        yaml.safe_load((CONFIG_DIR / "agent.yaml").read_text())
        results.append(("конфиг agent.yaml", True, "валидный YAML"))
    except Exception as e:
        results.append(("конфиг agent.yaml", False, str(e)))

    # сервис
    if systemd_is_active("traffic-agent"):
        results.append(("traffic-agent", True, "запущен"))
    else:
        results.append(("traffic-agent", False, "не работает"))

    # связь с ботом
    bot_url = params["bot_url"].rstrip("/")
    code = http_probe(f"{bot_url}/notify")
    if code == 401:
        results.append(("связь с ботом", True, "доступен (HTTP 401 — ожидаемо)"))
    elif code == 0:
        results.append(("связь с ботом", False, "Connection refused / таймаут"))
    else:
        results.append(("связь с ботом", None, f"HTTP {code} (соединение есть)"))

    show_check_results(results, "Агент")

    if not systemd_is_active("traffic-agent"):
        console.print()
        warn("Последние строки лога:")
        try:
            log = subprocess.check_output(
                ["journalctl", "-u", "traffic-agent", "-n", "15", "--no-pager"],
                text=True,
            )
            console.print(Panel(log, border_style="red"))
        except Exception:
            pass


# ============================================================
#  Установка — БОТ
# ============================================================

def write_bot_config(p: dict) -> None:
    cfg = CONFIG_DIR / "bot.yaml"
    chat_lines = "\n".join(f"  - {cid}" for cid in p["chat_ids"])
    cfg.write_text(f"""# Сгенерировано install.py {datetime.now().isoformat()}
telegram_token: "{p['telegram_token']}"
telegram_api_base: "{p['telegram_api_base']}"
chat_ids:
{chat_lines}
listen_host: "{p['listen_host']}"
listen_port: {p['listen_port']}
shared_secret: "{p['shared_secret']}"
daily_report_hour: {p['daily_report_hour']}
agent_silence_alert: 7200
""")
    cfg.chmod(0o600)
    ok(f"Конфиг создан: [muted]{cfg}[/muted]")


def install_bot_flow() -> None:
    if is_installed("bot"):
        warn("Бот уже установлен")
        if not Confirm.ask("Переустановить (с перезаписью конфига)?", default=False):
            return

    params = collect_bot_params()

    console.print()
    console.rule("[title] Установка [/title]", style="cyan")

    install_packages(["python3", "python3-pip", "python3-yaml",
                      "python3-aiohttp", "ufw", "git", "openssl", "curl"])

    # aiohttp гарантированно
    if not run_quiet(["python3", "-c", "import aiohttp"]):
        step("Ставлю aiohttp через pip")
        for cmd in [
            ["pip3", "install", "--break-system-packages", "aiohttp"],
            ["pip3", "install", "aiohttp"],
        ]:
            if run_quiet(cmd):
                break
        else:
            die("Не удалось установить aiohttp")
    ok("aiohttp доступен")

    ensure_repo()
    ensure_dirs()
    write_bot_config(params)

    step("Копирую файлы")
    run(["install", "-m", "0755", str(REPO_DIR / "bot/bot.py"),
         str(BIN_DIR / "traffic-bot")])
    run(["install", "-m", "0644", str(REPO_DIR / "bot/traffic-bot.service"),
         str(SYSTEMD_DIR / "traffic-bot.service")])
    run(["systemctl", "daemon-reload"])
    ok("Файлы установлены")

    # Настройка файрвола
    configure_firewall_for_bot(params)

    step("Запускаю traffic-bot")
    run(["systemctl", "enable", "--now", "traffic-bot"], capture=True)
    time.sleep(4)

    console.print()
    verify_bot(params)


def configure_firewall_for_bot(params: dict) -> None:
    """Открывает порт бота в UFW в зависимости от выбора пользователя."""
    port = params["listen_port"]
    listen_host = params["listen_host"]

    console.print()
    console.rule("[title] Настройка файрвола [/title]", style="cyan")

    # Случай 1: бот слушает только локально → файрвол не нужен
    if listen_host in ("127.0.0.1", "localhost", "::1"):
        ok(f"Бот слушает на [accent]{listen_host}[/accent] — внешний доступ не нужен, UFW не трогаю")
        return

    # Случай 2: UFW не установлен/не активен
    if not ufw_installed():
        warn("UFW не установлен в системе — пропускаю настройку файрвола")
        return

    if not ufw_active():
        console.print(Panel(
            f"[warn]UFW установлен, но не активен.[/warn]\n\n"
            f"Это значит, что порт [accent]{port}[/accent] сейчас доступен всем "
            f"(если только нет другого файрвола, например на стороне хостинга).\n\n"
            f"Чтобы включить UFW позже, выполните:\n"
            f"  [accent]ufw allow OpenSSH[/accent]       (или 'ufw allow <ваш SSH-порт>/tcp')\n"
            f"  [accent]ufw allow {port}/tcp[/accent]\n"
            f"  [accent]ufw enable[/accent]\n\n"
            f"[err]ВАЖНО:[/err] перед [accent]ufw enable[/accent] обязательно "
            f"разрешите SSH-порт — иначе потеряете доступ.",
            title="⚠️  UFW неактивен", border_style="yellow",
        ))
        return

    # Случай 3: UFW активен — спрашиваем стратегию
    console.print("🔥 [prompt]UFW активен.[/prompt] Как открыть порт?")
    t = Table(box=box.SIMPLE, show_header=False)
    t.add_column("", style="accent")
    t.add_column("Описание")
    t.add_row("1", "Открыть всем (быстро, но небезопасно)")
    t.add_row("2", "Открыть только указанным IP агентов (рекомендуется)")
    t.add_row("3", "Не открывать сейчас (например, бот за reverse-proxy)")
    console.print(t)
    choice = Prompt.ask("Выбор", choices=["1", "2", "3"], default="2")

    if choice == "3":
        warn(f"Файрвол не настроен. Откройте порт {port} вручную позже:")
        console.print(f"  [accent]ufw allow from <AGENT_IP> to any port {port} proto tcp[/accent]")
        return

    if choice == "1":
        console.print(Panel(
            f"[warn]Открываю порт {port} для всех.[/warn]\n\n"
            f"Бот защищён [accent]shared_secret[/accent], но кто угодно сможет "
            f"стучаться, что засорит логи и может стать вектором атаки.",
            title="⚠️  Внимание", border_style="yellow",
        ))
        if not Confirm.ask("Точно открыть всем?", default=False):
            return configure_firewall_for_bot(params)  # повторим выбор
        success, log_text = ufw_open_port(port, sources=None,
                                          comment=f"traffic-bot port")
        if success:
            ok(f"Порт {port} открыт всем")
        else:
            err(f"Ошибка UFW: {log_text}")
        return

    # choice == "2" — список IP агентов
    console.print(f"\n💡 Введите IP-адреса агентов, которым разрешить подключение к порту [accent]{port}[/accent].")
    console.print("[muted]   Можно вводить отдельные IP (1.2.3.4) или подсети (10.0.0.0/24).[/muted]")
    sources: list[str] = []
    while True:
        ip = Prompt.ask(
            f"   IP агента #{len(sources) + 1} [muted](Enter — закончить)[/muted]",
            default="", show_default=False,
        )
        ip = ip.strip()
        if not ip:
            if sources:
                break
            warn("Список пуст — нужен хотя бы один IP, иначе агенты не смогут подключиться")
            if Confirm.ask("Пропустить настройку UFW?", default=False):
                return
            continue
        if not validate_ip_or_cidr(ip):
            warn(f"Неверный формат: {ip}. Пример: 1.2.3.4 или 10.0.0.0/24")
            continue
        sources.append(ip)
        ok(f"Добавлен: [accent]{ip}[/accent]")

    success, log_text = ufw_open_port(port, sources=sources, comment="traffic-bot agent")
    console.print(log_text)
    if success:
        ok(f"Порт {port} открыт для {len(sources)} IP-адреса(ов)")
    else:
        err(f"Не удалось открыть порт: {log_text}")


def verify_bot(params: dict) -> None:
    console.rule("[title] Проверка [/title]", style="cyan")
    results = []

    try:
        import yaml
        yaml.safe_load((CONFIG_DIR / "bot.yaml").read_text())
        results.append(("конфиг bot.yaml", True, "валидный YAML"))
    except Exception as e:
        results.append(("конфиг bot.yaml", False, str(e)))

    if systemd_is_active("traffic-bot"):
        results.append(("traffic-bot", True, "запущен"))
    else:
        results.append(("traffic-bot", False, "не работает"))

    port = params["listen_port"]
    try:
        out = subprocess.check_output(["ss", "-tln"], text=True)
        if f":{port} " in out:
            results.append(("порт", True, f"слушает {port}"))
        else:
            results.append(("порт", False, f"никто не слушает {port}"))
    except Exception:
        results.append(("порт", False, "не удалось проверить"))

    code = http_probe(f"http://127.0.0.1:{port}/notify")
    if code == 401:
        results.append(("HTTP API", True, "отвечает (HTTP 401)"))
    else:
        results.append(("HTTP API", False, f"HTTP {code} (ожидался 401)"))

    show_check_results(results, "Бот")

    if not systemd_is_active("traffic-bot"):
        console.print()
        warn("Последние строки лога:")
        try:
            log = subprocess.check_output(
                ["journalctl", "-u", "traffic-bot", "-n", "20", "--no-pager"],
                text=True,
            )
            console.print(Panel(log, border_style="red"))
        except Exception:
            pass


# ============================================================
#  Общие — таблица проверок и финал
# ============================================================

def show_check_results(results: list[tuple], name: str) -> None:
    t = Table(box=box.ROUNDED, show_lines=False)
    t.add_column("Проверка", style="info")
    t.add_column("Статус", justify="center", width=8)
    t.add_column("Детали", style="muted")
    for check, status, details in results:
        if status is True:
            icon = "[ok]✓[/ok]"
        elif status is False:
            icon = "[err]✗[/err]"
        else:
            icon = "[warn]?[/warn]"
        t.add_row(check, icon, details)
    console.print(t)

    fails = [r for r in results if r[1] is False]
    console.print()
    if not fails:
        console.print(Panel(
            f"[ok]🎉 {name.upper()} УСТАНОВЛЕН УСПЕШНО[/ok]\n\n"
            f"Логи:   [accent]journalctl -u traffic-{name.lower()} -f[/accent]\n"
            f"Конфиг: [accent]/etc/traffic_monitor/{name.lower()}.yaml[/accent]",
            border_style="green", expand=False,
        ))
    else:
        console.print(Panel(
            f"[err]Установка завершилась с {len(fails)} ошибками[/err]\n\n"
            f"Что не так:\n" + "\n".join(f"  • {r[0]}: {r[2]}" for r in fails),
            border_style="red", expand=False,
        ))


# ============================================================
#  Удаление, статус, логи
# ============================================================

def uninstall_flow(component: str) -> None:
    svc = f"traffic-{component}"
    if not is_installed(component):
        warn(f"{svc} не установлен")
        return

    console.print()
    if not Confirm.ask(f"🗑  Удалить [accent]{svc}[/accent]?", default=False):
        return

    step(f"Останавливаю {svc}")
    run_quiet(["systemctl", "disable", "--now", svc])
    (BIN_DIR / svc).unlink(missing_ok=True)
    (SYSTEMD_DIR / f"{svc}.service").unlink(missing_ok=True)
    run(["systemctl", "daemon-reload"])

    cfg = CONFIG_DIR / f"{component}.yaml"
    if cfg.exists() and Confirm.ask(f"Удалить конфиг [muted]{cfg}[/muted]?", default=False):
        cfg.unlink()

    ok(f"{svc} удалён")


def show_status() -> None:
    console.print()
    t = Table(title="📊 Статус компонентов", box=box.ROUNDED)
    t.add_column("Компонент", style="info")
    t.add_column("Состояние", justify="center")
    t.add_column("Конфиг", style="muted")

    for component in ("bot", "agent"):
        svc = f"traffic-{component}"
        cfg = CONFIG_DIR / f"{component}.yaml"
        if is_installed(component):
            if systemd_is_active(svc):
                state = "[ok]● running[/ok]"
            else:
                state = "[err]● stopped[/err]"
        else:
            state = "[muted]○ не установлен[/muted]"
        cfg_str = f"✓ {cfg}" if cfg.exists() else "—"
        t.add_row(svc, state, cfg_str)
    console.print(t)
    console.print()


def show_logs() -> None:
    console.print()
    choices = []
    if is_installed("bot"):
        choices.append("bot")
    if is_installed("agent"):
        choices.append("agent")
    if not choices:
        warn("Нечего показывать — ничего не установлено")
        return

    component = Prompt.ask("Какой лог?", choices=choices)
    svc = f"traffic-{component}"
    console.print()
    console.rule(f"[title] Логи {svc} (последние 50 строк) [/title]")
    try:
        log = subprocess.check_output(
            ["journalctl", "-u", svc, "-n", "50", "--no-pager"],
            text=True,
        )
        console.print(log)
    except Exception as e:
        err(f"Ошибка чтения лога: {e}")
    console.print()


# ============================================================
#  Главное меню
# ============================================================

def main_menu() -> None:
    while True:
        console.print()
        t = Table(box=box.DOUBLE_EDGE, title="🛠  Главное меню",
                  show_header=False, title_style="bold cyan")
        t.add_column("", style="accent", width=4)
        t.add_column("", width=40)
        t.add_row("1", "📡 Установить АГЕНТ")
        t.add_row("2", "🤖 Установить БОТ")
        t.add_row("3", "🎯 Установить ОБА (на одной машине)")
        t.add_row("", "")
        t.add_row("4", "📊 Показать статус")
        t.add_row("5", "📜 Показать логи")
        t.add_row("", "")
        t.add_row("6", "🗑  Удалить агент")
        t.add_row("7", "🗑  Удалить бот")
        t.add_row("", "")
        t.add_row("0", "🚪 Выход")
        console.print(t)

        choice = Prompt.ask("\n[prompt]Выбор[/prompt]",
                            choices=["0", "1", "2", "3", "4", "5", "6", "7"],
                            default="1")

        if choice == "1":
            install_agent_flow()
        elif choice == "2":
            install_bot_flow()
        elif choice == "3":
            install_bot_flow()
            install_agent_flow()
        elif choice == "4":
            show_status()
        elif choice == "5":
            show_logs()
        elif choice == "6":
            uninstall_flow("agent")
        elif choice == "7":
            uninstall_flow("bot")
        elif choice == "0":
            console.print("\n[accent]До встречи! 👋[/accent]\n")
            break


def main() -> None:
    require_root()
    banner()

    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg in ("--help", "-h"):
        console.print(Markdown("""
**Использование:**

- `sudo python3 install.py` — интерактивное меню
- `sudo python3 install.py agent` — установить агента
- `sudo python3 install.py bot` — установить бота
- `sudo python3 install.py both` — установить и бота, и агента
"""))
        return

    if arg == "agent":
        install_agent_flow()
    elif arg == "bot":
        install_bot_flow()
    elif arg == "both":
        install_bot_flow()
        install_agent_flow()
    elif arg:
        die(f"Неизвестная команда: {arg}")
    else:
        main_menu()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[warn]Прервано пользователем[/warn]\n")
        sys.exit(130)
