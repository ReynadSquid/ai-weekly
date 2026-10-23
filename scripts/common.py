"""Общие утилиты пайплайна: пути, конфиги, время, логирование с маскировкой секретов."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(os.environ.get("AIW_ROOT", Path(__file__).resolve().parent.parent))
CONFIG = ROOT / "config"
STATE = ROOT / "state"
RUNS = ROOT / "runs"
ARCHIVE = ROOT / "archive"

SECRET_ENV = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHANNEL_ID",
    "TELEGRAM_TEST_CHAT_ID",
    "TELEGRAM_ADMIN_CHAT_ID",
)
_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 AIWeeklyBot/1.0"
)


def setup_stdio() -> None:
    """UTF-8 в консоли (важно для Windows)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def load_dotenv(path: Path | None = None) -> None:
    """Минимальный загрузчик .env: не перезаписывает уже заданные переменные."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def mask(text: str) -> str:
    """Убирает токен бота из любой строки перед выводом/записью в лог."""
    if not text:
        return text
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        text = text.replace(token, "***")
    return _TOKEN_RE.sub("***", text)


# ---------- конфиги ----------

def load_yaml(name: str) -> Any:
    import yaml  # локальный импорт: тестам без конфигов yaml не нужен

    path = CONFIG / name
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


# ---------- время ----------

def tz():
    from zoneinfo import ZoneInfo

    name = (load_yaml("schedule.yaml") or {}).get("timezone", "Asia/Krasnoyarsk")
    return ZoneInfo(name)


def now_utc() -> datetime:
    override = os.environ.get("AIW_NOW")  # для тестов и ручной отладки
    if override:
        return parse_dt(override)
    return datetime.now(timezone.utc)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- URL ----------

_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|ref$|ref_src$|mc_|igshid$|si$)")


def normalize_url(url: str) -> str:
    """Канонизация URL для сравнения дублей: без трекинга, якоря, www и хвостового слэша."""
    url = (url or "").strip()
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = urlencode(
        sorted((k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k.lower()))
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, query, ""))


def url_id(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()


def domain(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# ---------- логирование запуска ----------

class RunLog:
    """Пишет в runs/<id>/run.log строки «время | шаг | сообщение»; токен маскируется."""

    def __init__(self, run_dir: Path):
        self.path = run_dir / "run.log"
        run_dir.mkdir(parents=True, exist_ok=True)

    def __call__(self, step: str, message: str) -> None:
        line = f"{iso(now_utc())} | {step:<10} | {mask(message)}"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, file=sys.stderr)


def run_dir(run_id: str) -> Path:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}_[a-z]+", run_id):
        raise SystemExit(f"Некорректный run id: {run_id!r}")
    return RUNS / run_id
