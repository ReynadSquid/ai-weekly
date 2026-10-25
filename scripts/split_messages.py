"""Нарезка выпуска на сообщения Telegram (≤ 4096 символов) и валидация HTML.

Использование:
    python scripts/split_messages.py --run 2026-10-25_weekly [--limit 4000]

Вход: runs/<id>/posts.json
    {"issue_id": "...", "header": "<b>AI Weekly #1 · ...</b>\\n\\n• ...",
     "sections": [{"key": "A", "title": "🏢 Лидеры рынка",
                   "items": [{"id": "<id кандидата>", "html": "<b>…</b>\\n…\\n<a href=\\"…\\">Источник</a>"}]}],
     "footer": "Вывод недели…\\n\\n#OpenAI #AIWeekly"}
Выход: runs/<id>/messages.json — {"issue_id", "messages": [{"index", "text", "disable_preview", "length"}]}
Код выхода 1 — ошибки валидации (печатаются в JSON).

Правила: режем по границам разделов; если раздел не влезает целиком — между новостями,
заголовок раздела повторяется с пометкой «(продолжение)». Новость никогда не разрывается.
Длина считается по видимому тексту (без тегов, с раскрытыми сущностями) в UTF-16 —
так же строго или строже, чем считает Telegram.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from html.parser import HTMLParser

from common import read_json, run_dir, setup_stdio, write_json

TG_LIMIT = 4096
DEFAULT_LIMIT = 4000  # запас на случай расхождений в подсчёте

ALLOWED = {
    "b": set(), "strong": set(), "i": set(), "em": set(), "u": set(), "ins": set(),
    "s": set(), "strike": set(), "del": set(), "a": {"href"}, "code": {"class"},
    "pre": set(), "blockquote": {"expandable"}, "tg-spoiler": set(), "span": {"class"},
}
_BAD_LT = re.compile(r"<(?!/?[a-zA-Z][a-zA-Z0-9-]*[\s>/])")
_BAD_AMP = re.compile(r"&(?!(lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+);)")


def visible_len(text: str) -> int:
    plain = html.unescape(re.sub(r"<[^>]+>", "", text))
    return len(plain.encode("utf-16-le")) // 2


class _Validator(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED:
            self.errors.append(f"запрещённый тег <{tag}>")
            return
        for name, value in attrs:
            if name not in ALLOWED[tag]:
                self.errors.append(f"запрещённый атрибут {name} у <{tag}>")
            if tag == "a" and name == "href" and not re.match(r"^(https?|tg|mailto):", value or ""):
                self.errors.append(f"некорректный href: {value!r}")
            if tag == "span" and name == "class" and value != "tg-spoiler":
                self.errors.append("<span> допустим только с class=\"tg-spoiler\"")
        if tag == "a" and not any(n == "href" for n, _ in attrs):
            self.errors.append("<a> без href")
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.errors.append(f"самозакрывающийся тег <{tag}/> не поддерживается")

    def handle_endtag(self, tag):
        if tag not in ALLOWED:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"непарный закрывающий тег </{tag}>")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()


def validate_html(text: str) -> list[str]:
    errors = []
    if _BAD_LT.search(text):
        errors.append("неэкранированный символ '<' (нужно &lt;)")
    if _BAD_AMP.search(text):
        errors.append("неэкранированный '&' или неподдерживаемая сущность (нужно &amp;)")
    v = _Validator()
    v.feed(text)
    v.close()
    errors += v.errors
    if v.stack:
        errors.append(f"незакрытые теги: {v.stack}")
    return errors


def split_issue(posts: dict, limit: int = DEFAULT_LIMIT) -> list[str]:
    sep = "\n\n"
    blocks: list[str] = []  # готовые сообщения
    cur = ""

    def fits(a: str, b: str) -> bool:
        return visible_len(a + sep + b if a else b) <= limit

    def push(block: str):
        nonlocal cur
        if fits(cur, block):
            cur = cur + sep + block if cur else block
        else:
            if cur:
                blocks.append(cur)
            cur = block

    header = (posts.get("header") or "").strip()
    if header:
        if visible_len(header) > limit:
            raise ValueError("заголовок выпуска длиннее лимита сообщения")
        cur = header

    for section in posts.get("sections", []):
        items = [i["html"].strip() for i in section.get("items", []) if i.get("html", "").strip()]
        if not items:
            continue
        title = f"<b>{section['title']}</b>" if section.get("title") else ""
        whole = sep.join(([title] if title else []) + items)
        if fits(cur, whole):
            push(whole)
            continue
        if visible_len(whole) <= limit:  # раздел целиком — в новое сообщение
            if cur:
                blocks.append(cur)
            cur = whole
            continue
        # раздел длиннее сообщения — режем между новостями
        if cur:
            blocks.append(cur)
        cur = title
        cont = f"<b>{section['title']} (продолжение)</b>" if title else ""
        for item in items:
            if visible_len(item) > limit - (visible_len(cont) + 2 if cont else 0):
                raise ValueError(f"новость не помещается в одно сообщение: {item[:80]!r}")
            if fits(cur, item):
                cur = cur + sep + item if cur else item
            else:
                blocks.append(cur)
                cur = cont + sep + item if cont else item

    footer = (posts.get("footer") or "").strip()
    if footer:
        push(footer)
    if cur:
        blocks.append(cur)
    return blocks


POST_TAG = "#AIWeekly"  # хэштег под каждым постом — для поиска по каналу
TAG_RESERVE = 300  # место под строку хэштегов в каждом сообщении


def with_tag(text: str, tag: str = POST_TAG) -> str:
    return text if not tag or tag in text else f"{text}\n\n{tag}"


def company_hashtag(company: str | None, hashtag: str | None = None) -> str | None:
    """Хэштег компании: из watchlist (поле hashtag), иначе из латинского названия («Moonshot AI» → #MoonshotAI)."""
    if hashtag:
        return hashtag
    if company and re.fullmatch(r"[A-Za-z0-9 .&+\-]+", company):
        tag = re.sub(r"[^A-Za-z0-9]", "", company)
        return f"#{tag}" if tag else None
    return None


def tag_line(tags: list[str | None]) -> str:
    return " ".join(dict.fromkeys([t for t in tags if t] + [POST_TAG]))


def separate_title(item_html: str) -> str:
    """Пустая строка между жирным заголовком новости и её текстом — заголовок визуально отделён."""
    return re.sub(r"^(\s*<b>.*?</b>)\n(?!\n)", r"\1\n\n", item_html, count=1, flags=re.S)


MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
              "октября", "ноября", "декабря"]
_WHY = re.compile(r"^\s*<i>\s*Почему важно:?\s*</i>\s*:?\s*")
_SRC = re.compile(r'^\s*<a href="([^"]+)">\s*(?:→\s*)?Источник\s*</a>\s*$')


def date_label(value: str | None) -> str | None:
    """«2026-10-22» или ISO-время → «22 октября»."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value or "")
    return f"{int(m.group(3))} {MONTHS_GEN[int(m.group(2)) - 1]}" if m else None


DATE_PREFIX = "Дата публикации: "


def decorate_item(item_html: str, number: int | None = None, date: str | None = None,
                  compact: bool = False) -> str:
    """Оформление новости (вариант B):
        <b>N. Заголовок</b>
        <i>Дата публикации: 22 октября</i>
                                              ← нет в compact
        Текст.
                                              ← нет в compact
        <blockquote><b>Почему важно:</b> …</blockquote>
        <a href="…">→ Источник</a>
    Если новость не по шаблону (нет жирного заголовка в первой строке) — остаётся как есть."""
    lines = [l for l in item_html.strip().split("\n")]
    m = re.match(r"^\s*<b>(.*?)</b>\s*$", lines[0] if lines else "")
    if not m:
        return separate_title(item_html)
    title = m.group(1).strip()
    if number is not None and not re.match(r"^\d+\.\s", title):
        title = f"{number}. {title}"
    head = [f"<b>{title}</b>"] + ([f"<i>{DATE_PREFIX}{date}</i>"] if date else [])
    gap = "\n" if compact else "\n\n"
    body, why, src = [], None, None
    for line in lines[1:]:
        if _WHY.match(line) and why is None:
            why = _WHY.sub("", line).strip()
        elif _SRC.match(line):
            src = _SRC.match(line).group(1)
        elif line.strip():
            body.append(line)
    out = "\n".join(head) + (gap if date else "\n\n") + "\n".join(body)
    if why:
        out += f"{gap}<blockquote><b>Почему важно:</b> {why}</blockquote>"
    if src:
        out += f'\n<a href="{src}">→ Источник</a>'
    return out


def _tags_of(value) -> list[str]:
    if not value:
        return []
    return [value] if isinstance(value, str) else [t for t in value if t]


def _is_hashtag_line(line: str) -> bool:
    words = line.split()
    return bool(words) and all(w.startswith("#") for w in words)


def drop_hashtag_lines(text: str) -> str:
    """Хэштеги всего выпуска из подвала редактора не нужны — у каждого сообщения своя строка."""
    return "\n".join(l for l in (text or "").splitlines() if not _is_hashtag_line(l)).strip()


def build_messages(posts: dict, limit: int = DEFAULT_LIMIT,
                   item_tags: dict[str, object] | None = None,
                   item_dates: dict[str, str | None] | None = None) -> tuple[list[dict], list[str]]:
    """Нарезка + под каждым сообщением строка хэштегов компаний его новостей и #AIWeekly.
    item_tags: id новости → хэштег или список хэштегов (компании новости и упомянутые в ней).
    item_dates: id новости → дата («22 октября»). Новости нумеруются сквозь весь выпуск."""
    errors: list[str] = []
    for part, text in [("header", posts.get("header", "")), ("footer", posts.get("footer", ""))]:
        errors += [f"{part}: {e}" for e in validate_html(text or "")]
    for s in posts.get("sections", []):
        for item in s.get("items", []):
            errors += [f"{s.get('key')}/{item.get('id')}: {e}" for e in validate_html(item.get("html", ""))]
    item_dates = item_dates or {}
    counter = iter(range(1, 10_000))
    posts = dict(posts, footer=drop_hashtag_lines(posts.get("footer", "")),
                 sections=[dict(s, items=[dict(i, html=decorate_item(i["html"], next(counter),
                                                                     item_dates.get(i.get("id"))))
                                          for i in s.get("items", []) if i.get("html", "").strip()])
                           for s in posts.get("sections", [])])
    items = [i for s in posts.get("sections", []) for i in s.get("items", []) if i.get("html", "").strip()]
    item_tags = item_tags or {}
    try:
        texts = split_issue(posts, limit - TAG_RESERVE)
    except ValueError as e:
        return [], errors + [str(e)]
    messages = []
    for i, t in enumerate(texts):
        tags = [tag for it in items if it["html"].strip() in t for tag in _tags_of(item_tags.get(it.get("id")))]
        t = f"{t}\n\n{tag_line(tags)}"
        n = visible_len(t)
        if n > TG_LIMIT:
            errors.append(f"сообщение {i}: {n} > {TG_LIMIT}")
        errors += [f"сообщение {i}: {e}" for e in validate_html(t)]
        messages.append({"index": i, "text": t, "disable_preview": i > 0, "length": n})
    return messages, errors


CAPTION_LIMIT = 1024


def build_cover_variant(posts: dict, limit: int = DEFAULT_LIMIT,
                        item_tags: dict[str, object] | None = None,
                        item_dates: dict[str, str | None] | None = None) -> tuple[dict | None, str | None]:
    """Вариант с обложкой: подпись к фото — только строка-заголовок выпуска («AI Weekly #N · даты») и
    хэштег; три главных события не дублируются (обложка уже показывает, о ком выпуск). Дальше разделы
    без шапки и без превью ссылок. Возвращает (variant, причина_почему_нет); отсутствие варианта —
    не ошибка: выпуск уйдёт без обложки."""
    title = next((l.strip() for l in (posts.get("header") or "").splitlines() if l.strip()), "")
    if not title:
        return None, "нет шапки для подписи"
    header = with_tag(title)
    if visible_len(header) > CAPTION_LIMIT:
        return None, f"подпись {visible_len(header)} > {CAPTION_LIMIT} символов"
    if validate_html(header):
        return None, "заголовок выпуска не проходит валидацию HTML"
    messages, errors = build_messages(dict(posts, header=""), limit, item_tags, item_dates)
    if errors or not messages:
        return None, "; ".join(errors) or "нет сообщений"
    for m in messages:
        m["disable_preview"] = True
    return {"caption": header, "messages": messages}, None


def main(argv=None) -> int:
    setup_stdio()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", required=True)
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = ap.parse_args(argv)
    rd = run_dir(args.run)
    posts = read_json(rd / "posts.json")
    if not posts:
        print(json.dumps({"ok": False, "errors": ["нет posts.json"]}, ensure_ascii=False))
        return 1
    limit = min(args.limit, TG_LIMIT)
    from companies import Directory

    ed = read_json(rd / "editor_input.json") or {}
    html_by_id = {i.get("id"): i.get("html", "") for s in posts.get("sections", []) for i in s.get("items", [])}
    directory = Directory()
    item_tags = {it["id"]: directory.item_tags(it, html_by_id.get(it["id"], "")) for it in ed.get("items", [])}
    # дата новости: подтверждённая фактчекером дата события, иначе дата публикации у сборщика
    fc = {f["id"]: f for f in (read_json(rd / "factcheck.json") or {}).get("items", [])}
    item_dates = {it["id"]: date_label((fc.get(it["id"]) or {}).get("event_date") or it.get("published_at"))
                  for it in ed.get("items", [])}
    messages, errors = build_messages(posts, limit, item_tags, item_dates)
    out = {"issue_id": posts.get("issue_id", args.run), "messages": messages, "errors": errors}
    cover_note = None
    if (read_json(rd / "init.json") or {}).get("cover") and not errors:
        out["cover"], cover_note = build_cover_variant(posts, limit, item_tags, item_dates)
    write_json(rd / "messages.json", out)
    print(json.dumps({"ok": not errors, "messages": len(messages),
                      "lengths": [m["length"] for m in messages], "errors": errors,
                      "cover_variant": bool(out.get("cover")), "cover_note": cover_note},
                     ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
