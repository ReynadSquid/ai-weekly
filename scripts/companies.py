"""Справочник компаний для хэштегов и обложки: разбор составных названий и поиск упоминаний в тексте.

Источники: config/watchlist.yaml (name, hashtag, logo) и logo_aliases (компании вне списка).
"""
from __future__ import annotations

import html
import re

from common import load_yaml

_SPLIT = re.compile(r"\s*(?:/|,|;|&|\+|\sи\s|\sand\s)\s*")
_LATIN = re.compile(r"[A-Za-z0-9 .&+\-]+")


def split_companies(name: str | None) -> list[str]:
    """«NVIDIA / Google Cloud» → ["NVIDIA", "Google Cloud"]."""
    return [p.strip() for p in _SPLIT.split(name or "") if p and p.strip()]


def derived_hashtag(name: str) -> str | None:
    """Хэштег из латинского названия: «Moonshot AI» → #MoonshotAI; кириллица/служебные группы → None."""
    if name and _LATIN.fullmatch(name):
        tag = re.sub(r"[^A-Za-z0-9]", "", name)
        return f"#{tag}" if tag else None
    return None


class Directory:
    def __init__(self, watchlist: dict | None = None):
        wl = watchlist if watchlist is not None else (load_yaml("watchlist.yaml") or {})
        self.tags: dict[str, str] = {}      # имя (lower) → хэштег
        self.names: list[str] = []          # имена для поиска упоминаний в тексте (латиница)
        slug_tag: dict[str, str] = {}
        for c in wl.get("companies", []):
            tag = c.get("hashtag") or derived_hashtag(c["name"])
            if tag:
                self.tags[c["name"].lower()] = tag
                if c.get("logo"):
                    slug_tag.setdefault(c["logo"], tag)
            if _LATIN.fullmatch(c["name"]):
                self.names.append(c["name"])
        for alias, slug in (wl.get("logo_aliases") or {}).items():
            tag = slug_tag.get(slug) or derived_hashtag(alias)
            slug_tag.setdefault(slug, tag)
            if tag:
                self.tags.setdefault(alias.lower(), tag)
            self.names.append(alias)

    def hashtag(self, name: str | None) -> str | None:
        if not name:
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
        """Хэштеги новости: её компании (составные названия разбиваются) + упомянутые в тексте."""
        tags = [self.hashtag(c) for c in split_companies(item.get("company"))]
        if item.get("hashtag") and len(split_companies(item.get("company"))) <= 1:
            tags.insert(0, item["hashtag"])
        tags += self.mentions(html_text)
        return list(dict.fromkeys(t for t in tags if t))
