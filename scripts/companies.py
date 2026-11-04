"""Справочник компаний для хэштегов и обложки: единые названия, разбор составных строк, поиск упоминаний.

Сборщики пишут поле company как придётся: «Microsoft» и «microsoft», «NVIDIA / Google Cloud»,
«Индустрия (Google, Microsoft, OpenAI, Anthropic)», «n/a». Здесь всё это приводится к списку
канонических названий из config/watchlist.yaml (name) и logo_aliases, без дублей и мусора.
"""
from __future__ import annotations

import html
import re

from common import load_yaml

_SPLIT = re.compile(r"\s*(?:/|,|;|&|\+|\sи\s|\sand\s)\s*")
_LATIN = re.compile(r"[A-Za-z0-9 .&+\-]+")
# «нет компании»: такие значения не дают ни плитки, ни хэштега
PLACEHOLDERS = {"n/a", "na", "n.a.", "none", "null", "unknown", "-", "—", "–", "?", "нет", "не указано",
                "неизвестно", "various", "multiple", "разные", "несколько"}


def _clean(part: str) -> str:
    return part.strip().strip("()[]{}«»\"'.,;: ").strip()


def _is_placeholder(s: str) -> bool:
    return s.strip().lower() in PLACEHOLDERS


def split_companies(name: str | None) -> list[str]:
    """Разбор без справочника: «NVIDIA / Google Cloud» → ["NVIDIA", "Google Cloud"];
    «Индустрия (Google, Microsoft)» → ["Индустрия", "Google", "Microsoft"]; «n/a» → []."""
    if not name or _is_placeholder(name):
        return []
    flat = re.sub(r"[()\[\]]", ",", name)
    parts = [_clean(p) for p in _SPLIT.split(flat)]
    return [p for p in parts if p and not _is_placeholder(p)]


def derived_hashtag(name: str) -> str | None:
    """Хэштег из латинского названия: «Moonshot AI» → #MoonshotAI; кириллица, мусор, 1 буква → None."""
    if name and _LATIN.fullmatch(name) and not _is_placeholder(name):
        tag = re.sub(r"[^A-Za-z0-9]", "", name)
        return f"#{tag}" if len(tag) >= 2 else None
    return None


class Directory:
    def __init__(self, watchlist: dict | None = None):
        wl = watchlist if watchlist is not None else (load_yaml("watchlist.yaml") or {})
        self.tags: dict[str, str] = {}       # имя (lower) → хэштег
        self.canon: dict[str, str] = {}      # имя (lower) → каноническое название
        self.names: list[str] = []           # имена для поиска упоминаний в тексте (латиница)
        slug_tag: dict[str, str] = {}
        for c in wl.get("companies", []):
            self.canon[c["name"].lower()] = c["name"]
            tag = c.get("hashtag") or derived_hashtag(c["name"])
            if tag:
                self.tags[c["name"].lower()] = tag
                if c.get("logo"):
                    slug_tag.setdefault(c["logo"], tag)
            if _LATIN.fullmatch(c["name"]):
                self.names.append(c["name"])
        for alias, slug in (wl.get("logo_aliases") or {}).items():
            self.canon.setdefault(alias.lower(), alias)
            tag = slug_tag.get(slug) or derived_hashtag(alias)
            slug_tag.setdefault(slug, tag)
            if tag:
                self.tags.setdefault(alias.lower(), tag)
            self.names.append(alias)

    def companies(self, name: str | None) -> list[str]:
        """Канонические компании из поля company: известное имя целиком → оно; иначе разбор по
        разделителям и скобкам; служебные слова на кириллице («Индустрия») и заглушки («n/a») отброшены;
        регистр приводится к справочнику («microsoft» → «Microsoft»); без повторов."""
        if not name or _is_placeholder(name):
            return []
        whole = _clean(name)
        if whole.lower() in self.canon:
            return [self.canon[whole.lower()]]
        out = []
        for part in split_companies(name):
            known = self.canon.get(part.lower())
            if known:
                out.append(known)
            elif re.search(r"[A-Za-z]", part):
                out.append(part)          # компания вне справочника, латиница — оставляем как есть
            # кириллица без латиницы и не из справочника — служебное слово, пропускаем
        return list(dict.fromkeys(out))

    def hashtag(self, name: str | None) -> str | None:
        if not name or _is_placeholder(name):
            return None
        return self.tags.get(name.lower()) or derived_hashtag(name)

    def mentions(self, text_html: str) -> list[str]:
        """Хэштеги компаний, упомянутых в видимом тексте (без ссылок), по порядку появления.
        Совпадения с учётом регистра и границ слов; длинное имя побеждает вложенное (Google Cloud > Google)."""
        text = html.unescape(re.sub(r"<[^>]+>", " ", text_html or ""))
        found = []
        for name in self.names:
            for m in re.finditer(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", text):
                found.append((m.start(), -(m.end() - m.start()), m.end(), name))
        found.sort()
        out, last_end = [], -1
        for start, _, end, name in found:
            if start < last_end:
                continue  # внутри более длинного совпадения
            last_end = end
            tag = self.hashtag(name)
            if tag and tag not in out:
                out.append(tag)
        return out

    def item_tags(self, item: dict, html_text: str = "") -> list[str]:
        """Хэштеги новости: её компании (канонические) + упомянутые в тексте."""
        comps = self.companies(item.get("company"))
        tags = [self.hashtag(c) for c in comps]
        if item.get("hashtag") and len(comps) <= 1:
            tags.insert(0, item["hashtag"])
        tags += self.mentions(html_text)
        return list(dict.fromkeys(t for t in tags if t))
