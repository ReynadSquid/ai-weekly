"""Публикация выпуска в Telegram через Bot API. Это скрипт, а не LLM-агент.

Использование:
    python scripts/tg_publish.py --run 2026-10-25_weekly      # отправить runs/<id>/messages.json
    python scripts/tg_publish.py --report runs/<id>/report.txt # отчёт админу (TELEGRAM_ADMIN_CHAT_ID)
    python scripts/tg_publish.py --check                       # проверить токен и доступ к чатам

Режим (dry-run / live) берётся из runs/<id>/init.json:
    live    → TELEGRAM_CHANNEL_ID
    dry-run → TELEGRAM_TEST_CHAT_ID (если не задан — TELEGRAM_ADMIN_CHAT_ID)

Гарантии:
  * повторный запуск не отправляет уже отправленное: каждое сообщение записывается
    в state/published.json сразу после ответа Telegram;
  * 429 — ждём retry_after и повторяем; 5xx/сеть — 3 попытки;
  * 400/403 — стоп, код выхода 3, отправленные части остаются записанными;
  * пауза 3 с между сообщениями; превью ссылок только у первого сообщения;
  * токен никогда не печатается (маскируется).
Коды выхода: 0 — успех/уже опубликовано, 2 — ошибка конфигурации, 3 — ошибка Telegram.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from common import STATE, iso, load_dotenv, mask, now_utc, read_json, run_dir, setup_stdio, write_json

PUBLISHED = STATE / "published.json"
PAUSE_SEC = 3


class TelegramError(Exception):
    def __init__(self, code: int, description: str):
        super().__init__(f"Telegram {code}: {description}")
        self.code = code
        self.description = description


def _multipart(payload: dict) -> tuple[bytes, str]:
    """payload["_file"] = (поле, путь) → multipart/form-data; остальные поля — текстом/JSON."""
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in payload.items():
        if k == "_file":
            continue
        val = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{val}\r\n'.encode("utf-8"))
    field, path = payload["_file"]
    path = Path(path)
    mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'
                 f"Content-Type: {mime}\r\n\r\n".encode("utf-8") + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def api_call(method: str, payload: dict, *, token: str, timeout: float = 60) -> dict:
    base = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
    if "_file" in payload:
        data, ctype = _multipart(payload)
    else:
        data, ctype = json.dumps(payload).encode("utf-8"), "application/json"
    req = urllib.request.Request(
        f"{base}/bot{token}/{method}",
        data=data,
        headers={"Content-Type": ctype},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error_code": e.code, "description": mask(str(e))}


def call_with_retries(method: str, payload: dict, *, token: str, call=api_call,
                      sleep=time.sleep, max_429: int = 5, max_net: int = 3) -> dict:
    n429 = nnet = 0
    while True:
        try:
            resp = call(method, payload, token=token)
        except Exception as e:  # сеть/таймаут
            nnet += 1
            if nnet >= max_net:
                raise TelegramError(0, mask(f"{type(e).__name__}: {e}"))
            sleep(5 * nnet)
            continue
        if resp.get("ok"):
            return resp["result"]
        code = resp.get("error_code", 0)
        desc = mask(resp.get("description", ""))
        if code == 429 and n429 < max_429:
            n429 += 1
            sleep(int((resp.get("parameters") or {}).get("retry_after", 5)) + 1)
            continue
        if code >= 500 and nnet < max_net:
            nnet += 1
            sleep(5 * nnet)
            continue
        raise TelegramError(code, desc)


def send_message(text: str, chat_id: str, *, token: str, disable_preview: bool, parse_mode="HTML",
                 call=api_call, sleep=time.sleep) -> int:
    payload = {"chat_id": chat_id, "text": text,
               "link_preview_options": {"is_disabled": bool(disable_preview)}}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    result = call_with_retries("sendMessage", payload, token=token, call=call, sleep=sleep)
    return int(result["message_id"])


def send_photo(path: Path, caption: str, chat_id: str, *, token: str, call=api_call,
               sleep=time.sleep) -> int:
    payload = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML", "_file": ("photo", str(path))}
    result = call_with_retries("sendPhoto", payload, token=token, call=call, sleep=sleep)
    return int(result["message_id"])


def ensure_cover(run_id: str) -> Path:
    """Готовая обложка runs/<id>/cover.jpg или собрать её сейчас (scripts/make_cover.py)."""
    path = run_dir(run_id) / "cover.jpg"
    if not path.exists():
        import make_cover

        make_cover.make_for_run(run_id)
    return path


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def target_chat(mode: str) -> str | None:
    if mode == "live":
        return os.environ.get("TELEGRAM_CHANNEL_ID")
    return os.environ.get("TELEGRAM_TEST_CHAT_ID") or os.environ.get("TELEGRAM_ADMIN_CHAT_ID")


def public_link(chat_id: str, message_id: int) -> str | None:
    """Ссылка на сообщение, только если канал задан публичным @username (ID не сохраняем)."""
    if chat_id and chat_id.startswith("@"):
        return f"https://t.me/{chat_id[1:]}/{message_id}"
    return None


def publish(run_id: str, *, token: str, call=api_call, sleep=time.sleep, cover_maker=ensure_cover) -> dict:
    rd = run_dir(run_id)
    init = read_json(rd / "init.json") or {}
    mode = init.get("mode", "dry-run")
    data = read_json(rd / "messages.json") or {}
    msgs = data.get("messages") or []
    cover = data.get("cover")
    if not msgs:
        raise SystemExit("нет runs/<id>/messages.json — сначала split_messages.py")
    chat = target_chat(mode)
    if not chat:
        raise SystemExit(f"не задан чат для режима {mode} (TELEGRAM_CHANNEL_ID / TELEGRAM_TEST_CHAT_ID)")

    db = read_json(PUBLISHED, {"issues": {}})
    key = f"{run_id}:{mode}"
    rec = db["issues"].get(key)
    if rec and rec.get("status") == "published":
        return {"status": "already_published", "key": key, "messages": len(rec["messages"])}
    if not rec:
        rec = {"issue_id": run_id, "mode": mode, "issue_number": init.get("issue_number"),
               "status": "in_progress", "started_at": iso(now_utc()), "messages": []}
        db["issues"][key] = rec

    # Обложка: только первым сообщением. Если текст уже начал уходить — обложку больше не шлём.
    # Не собралась или Telegram её не принял — выпуск уходит без неё, шапка остаётся в первом посте.
    variant = rec.get("variant")
    if variant is None:
        variant = "plain"
        if cover and not rec["messages"]:
            try:
                path = cover_maker(run_id)
                mid = send_photo(path, cover["caption"], chat, token=token, call=call, sleep=sleep)
                rec["cover"] = {"message_id": mid, "sent_at": iso(now_utc())}
                rec["link"] = public_link(chat, mid)
                variant = "cover"
            except Exception as e:
                rec["cover_error"] = mask(f"{type(e).__name__}: {e}")[:300]
        rec["variant"] = variant
        write_json(PUBLISHED, db)
    if variant == "cover":
        msgs = cover["messages"]

    sent = {m["index"]: m for m in rec["messages"]}
    for i, m in sent.items():  # выпуск не должен поменяться между частями
        if i < len(msgs) and m.get("hash") != text_hash(msgs[i]["text"]):
            raise SystemExit(f"сообщение {i} уже отправлено в другой редакции — нужна ручная проверка")

    first_send = True
    for m in msgs:
        if m["index"] in sent:
            continue
        if not first_send or sent or rec.get("cover"):
            sleep(PAUSE_SEC)
        first_send = False
        try:
            mid = send_message(m["text"], chat, token=token, disable_preview=m["disable_preview"],
                               call=call, sleep=sleep)
        except TelegramError as e:
            rec["status"] = "failed"
            rec["error"] = str(e)
            write_json(PUBLISHED, db)
            return {"status": "failed", "key": key, "error": str(e),
                    "sent": len(rec["messages"]), "total": len(msgs)}
        rec["messages"].append({"index": m["index"], "message_id": mid, "hash": text_hash(m["text"]),
                                "sent_at": iso(now_utc())})
        if m["index"] == 0 and variant != "cover":
            rec["link"] = public_link(chat, mid)
        rec.pop("error", None)
        write_json(PUBLISHED, db)  # сразу, чтобы прерванный запуск не задублировал

    rec["status"] = "published"
    rec["finished_at"] = iso(now_utc())
    write_json(PUBLISHED, db)
    return {"status": "published", "key": key, "messages": len(rec["messages"]), "link": rec.get("link"),
            "cover": bool(rec.get("cover")), "cover_error": rec.get("cover_error")}


def send_report(text: str, *, token: str | None, call=api_call) -> bool:
    chat = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")
    if not token or not chat:
        print("TELEGRAM_ADMIN_CHAT_ID/TOKEN не заданы — отчёт только в лог", file=sys.stderr)
        return False
    for chunk_start in range(0, len(text), 3900):
        send_message(text[chunk_start:chunk_start + 3900], chat, token=token, disable_preview=True,
                     parse_mode=None, call=call)
    return True


def check(token: str) -> int:
    ok = True
    try:
        me = call_with_retries("getMe", {}, token=token)
        print(f"Бот: @{me.get('username')} — токен рабочий")
    except TelegramError as e:
        print(f"Токен не работает: {e}")
        return 3
    for var in ("TELEGRAM_CHANNEL_ID", "TELEGRAM_TEST_CHAT_ID", "TELEGRAM_ADMIN_CHAT_ID"):
        chat = os.environ.get(var)
        if not chat:
            print(f"{var}: не задан")
            ok = ok and var != "TELEGRAM_ADMIN_CHAT_ID"
            continue
        try:
            info = call_with_retries("getChat", {"chat_id": chat}, token=token)
            title = info.get("title") or info.get("username") or info.get("first_name")
            line = f"{var}: доступен ({info.get('type')}: {title})"
            if info.get("type") == "channel":
                member = call_with_retries("getChatMember", {"chat_id": chat, "user_id": me["id"]}, token=token)
                can_post = member.get("status") == "creator" or member.get("can_post_messages")
                line += " — бот может публиковать" if can_post else " — НЕТ права публикации!"
                ok = ok and bool(can_post)
            print(line)
        except TelegramError as e:
            print(f"{var}: ошибка — {e}")
            ok = False
    return 0 if ok else 3


def main(argv=None) -> int:
    setup_stdio()
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run")
    g.add_argument("--report", help="файл с текстом отчёта или '-' для stdin")
    g.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")

    if args.report:
        text = sys.stdin.read() if args.report == "-" else open(args.report, encoding="utf-8").read()
        try:
            send_report(text, token=token)
        except TelegramError as e:
            print(f"Отчёт не отправлен: {e}", file=sys.stderr)
            return 3
        return 0
    if not token:
        print("TELEGRAM_BOT_TOKEN не задан", file=sys.stderr)
        return 2
    if args.check:
        return check(token)
    result = publish(args.run, token=token)
    print(json.dumps(result, ensure_ascii=False))
    return {"published": 0, "already_published": 0}.get(result["status"], 3)


if __name__ == "__main__":
    sys.exit(main())
