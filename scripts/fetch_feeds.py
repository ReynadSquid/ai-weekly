"""Детерминированный сбор кандидатов: RSS/Atom, arXiv, Hacker News, GitHub, Hugging Face, t.me/s.

Использование:
    python scripts/fetch_feeds.py --run 2026-10-25_weekly --group companies
    python scripts/fetch_feeds.py --run 2026-10-25_weekly --group all
    python scripts/fetch_feeds.py --run 2026-10-25_weekly --group telegram

Окно сбора берётся из runs/<id>/init.json. Результат — runs/<id>/candidates_feeds_<group>.json:
    {"items": [...кандидаты...], "errors": [{"source": ..., "error": ...}], "stats": {...}}
Поля summary_raw и importance заполняют LLM-агенты; скрипт кладёт исходный текст в raw_excerpt.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from html.parser import HTMLParser
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from common import (RunLog, domain, iso, load_yaml, now_utc, parse_dt, read_json, run_dir,
                    setup_stdio, url_id, write_json)
from net import FetchError, get_with_retries

ATOM = "{http://www.w3.org/2005/Atom}"
GROUPS = ("companies", "research", "media", "telegram")


def strip_html(text: str, limit: int = 600) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text[:limit]


def candidate(*, title, url, source, source_type, published_at, excerpt="", company=None,
              section=None, extra=None) -> dict:
    item = {
        "id": url_id(url),
        "title": strip_html(title, 300),
        "url": url.strip(),
        "source": source,
        "source_type": source_type,
        "published_at": iso(published_at) if published_at else None,
        "company": company,
        "product": None,
        "section": section,
        "summary_raw": "",
        "raw_excerpt": strip_html(excerpt),
        "importance": None,
        "found_by": "fetch_feeds",
    }
    if extra:
        item.update(extra)
    return item


# ---------- парсеры (чистые функции, покрыты тестами) ----------

def parse_rss(xml_text: str, src: dict) -> list[dict]:
    """RSS 2.0 и Atom."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    items = []
    if root.tag == f"{ATOM}feed":
        for e in root.findall(f"{ATOM}entry"):
            link = ""
            for l in e.findall(f"{ATOM}link"):
                if l.get("rel", "alternate") == "alternate":
                    link = l.get("href", "")
                    break
            date = e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated")
            summary = e.findtext(f"{ATOM}summary") or e.findtext(f"{ATOM}content") or ""
            items.append((e.findtext(f"{ATOM}title") or "", link, date, summary))
    else:
        for e in root.iter("item"):
            date = e.findtext("pubDate") or e.findtext("{http://purl.org/dc/elements/1.1/}date")
            items.append((e.findtext("title") or "", (e.findtext("link") or "").strip(), date,
                          e.findtext("description") or ""))
    out = []
    for title, link, date, summary in items:
        if not link or not title:
            continue
        out.append(candidate(title=title, url=link, source=src["name"], source_type=src["source_type"],
                             published_at=parse_dt(date), excerpt=summary, company=src.get("company"),
                             section=src.get("section")))
    return out


def parse_bing_news(xml_text, src: dict, company: str | None = None, section: str | None = None) -> list[dict]:
    """Лента Bing News: ссылка вида bing.com/news/apiclick.aspx?...&url=<статья> → берём прямую ссылку на статью."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    out = []
    for e in root.iter("item"):
        link = (e.findtext("link") or "").strip()
        real = parse_qs(urlsplit(link).query).get("url", [link])[0]
        title = e.findtext("title") or ""
        if not real.startswith("http") or not title:
            continue
        out.append(candidate(title=title, url=real, source=src["name"], source_type="media",
                             published_at=parse_dt(e.findtext("pubDate")), excerpt=e.findtext("description") or "",
                             company=company, section=section,
                             extra={"discovered_via": "bing.com/news", "publisher": domain(real)}))
    return out


# ---------- robots.txt ----------

SOFT_ERRORS: list[dict] = []  # частичные сбои источника (источник отработал, но не целиком)
ROBOTS_AGENT = "AIWeeklyBot"
_robots_cache: dict[str, object] = {}


def robots_allowed(url: str) -> bool:
    """Читаем только то, что разрешает robots.txt сайта (требование ТЗ). robots.txt недоступен → разрешено."""
    import urllib.robotparser

    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        try:
            resp = get_with_retries(base + "/robots.txt", retries=1, timeout=15)
            rp.parse(resp.text().splitlines())
        except Exception:
            rp = None
        _robots_cache[base] = rp
    rp = _robots_cache[base]
    return True if rp is None else rp.can_fetch(ROBOTS_AGENT, url)


def _get(url: str, **kw):
    if not robots_allowed(url):
        raise FetchError(f"{url}: robots.txt запрещает чтение")
    return get_with_retries(url, **kw)


def parse_arxiv(xml_text: str, src: dict) -> list[dict]:
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    out = []
    for e in root.findall(f"{ATOM}entry"):
        link = (e.findtext(f"{ATOM}id") or "").strip()
        link = re.sub(r"v\d+$", "", link).replace("http://", "https://")
        authors = [a.findtext(f"{ATOM}name") for a in e.findall(f"{ATOM}author")][:5]
        out.append(candidate(title=e.findtext(f"{ATOM}title") or "", url=link, source=src["name"],
                             source_type="research", published_at=parse_dt(e.findtext(f"{ATOM}published")),
                             excerpt=e.findtext(f"{ATOM}summary") or "", section="F",
                             extra={"authors": authors}))
    return out


def parse_hn(data: dict, src: dict, min_points: int) -> list[dict]:
    out = []
    for h in data.get("hits", []):
        if (h.get("points") or 0) < min_points:
            continue
        hn_url = f"https://news.ycombinator.com/item?id={h['objectID']}"
        url = h.get("url") or hn_url
        out.append(candidate(title=h.get("title") or "", url=url, source=src["name"],
                             source_type="aggregator", published_at=parse_dt(h.get("created_at")),
                             excerpt=f"{h.get('points')} points, {h.get('num_comments')} comments",
                             extra={"discovered_via": hn_url, "score": h.get("points")}))
    return out


def parse_hf_papers(data: list, src: dict, min_upvotes: int) -> list[dict]:
    out = []
    for row in data:
        paper = row.get("paper", row)
        upvotes = paper.get("upvotes") or row.get("upvotes") or 0
        if upvotes < min_upvotes:
            continue
        pid = paper.get("id")
        if not pid:
            continue
        out.append(candidate(title=paper.get("title") or row.get("title") or "",
                             url=f"https://arxiv.org/abs/{pid}", source=src["name"],
                             source_type="research",
                             published_at=parse_dt(row.get("publishedAt") or paper.get("publishedAt")),
                             excerpt=paper.get("summary") or "", section="F",
                             extra={"discovered_via": f"https://huggingface.co/papers/{pid}",
                                    "score": upvotes}))
    return out


def parse_hf_models(data: list, src: dict) -> list[dict]:
    out = []
    for m in data:
        mid = m.get("id") or m.get("modelId")
        if not mid:
            continue
        out.append(candidate(title=f"{mid} (Hugging Face trending)", url=f"https://huggingface.co/{mid}",
                             source=src["name"], source_type="research",
                             published_at=parse_dt(m.get("createdAt") or m.get("lastModified")),
                             excerpt=f"pipeline: {m.get('pipeline_tag')}, likes: {m.get('likes')}, "
                                     f"downloads: {m.get('downloads')}",
                             company=mid.split("/")[0], section="F",
                             extra={"score": m.get("likes")}))
    return out


def parse_github(data: dict, src: dict) -> list[dict]:
    out = []
    for r in data.get("items", []):
        out.append(candidate(title=f"{r['full_name']}: {r.get('description') or ''}", url=r["html_url"],
                             source=src["name"], source_type="research",
                             published_at=parse_dt(r.get("created_at")),
                             excerpt=f"stars: {r.get('stargazers_count')}, language: {r.get('language')}. "
                                     f"{r.get('description') or ''}",
                             section="F", extra={"score": r.get("stargazers_count")}))
    return out


class _TgParser(HTMLParser):
    """Разбор веб-превью t.me/s/<канал>: текст поста, дата, внешние ссылки."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.posts: list[dict] = []
        self._cur: dict | None = None
        self._text_depth = 0
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        self._depth += 1
        if tag == "div" and "tgme_widget_message " in cls + " " and a.get("data-post"):
            self._cur = {"post": a["data-post"], "text": "", "links": [], "date": None}
            self.posts.append(self._cur)
        if self._cur is None:
            return
        if tag == "div" and "tgme_widget_message_text" in cls:
            self._text_depth = self._depth
        if tag == "time" and a.get("datetime"):
            self._cur["date"] = a["datetime"]
        if tag == "a" and self._text_depth and a.get("href", "").startswith("http"):
            self._cur["links"].append(a["href"])
        if tag == "br" and self._text_depth:
            self._cur["text"] += "\n"

    def handle_endtag(self, tag):
        if self._text_depth and self._depth == self._text_depth:
            self._text_depth = 0
        self._depth -= 1

    def handle_data(self, data):
        if self._cur is not None and self._text_depth:
            self._cur["text"] += data


_SKIP_LINK_HOSTS = ("t.me", "telegram.me", "telegra.ph", "youtube.com", "youtu.be", "boosty.to",
                    "patreon.com", "instagram.com", "vk.com")


def parse_tg(html_text: str, channel: str) -> list[dict]:
    """Каждый пост с внешней ссылкой → кандидат с url первоисточника (первая внешняя ссылка).
    Посты без ссылок тоже возвращаются (url = пост), сборщик-агент ищет для них первоисточник."""
    p = _TgParser()
    p.feed(html_text)
    out = []
    for post in p.posts:
        text = re.sub(r"[ \t]+", " ", post["text"]).strip()
        if not text:
            continue
        links = [l for l in post["links"] if not domain(l).endswith(_SKIP_LINK_HOSTS)]
        post_url = f"https://t.me/{post['post']}"
        title = text.split("\n", 1)[0][:200]
        out.append(candidate(title=title, url=links[0] if links else post_url,
                             source=f"t.me/{channel}", source_type="telegram",
                             published_at=parse_dt(post["date"]), excerpt=text,
                             extra={"discovered_via": post_url, "other_links": links[1:6],
                                    "needs_primary_source": not links}))
    return out


# ---------- загрузка ----------

def fetch_source(src: dict, since: datetime, defaults: dict) -> list[dict]:
    kw = dict(retries=defaults.get("retries", 3), pause=defaults.get("retry_pause_sec", 5),
              timeout=defaults.get("timeout_sec", 20))
    params = src.get("params") or {}
    t = src["type"]
    if t == "rss":
        return parse_rss(_get(src["url"], **kw).body, src)
    if t == "arxiv":
        cats = " OR ".join(f"cat:{c}" for c in params.get("categories", ["cs.AI"]))
        q = urlencode({"search_query": cats, "sortBy": "submittedDate", "sortOrder": "descending",
                       "max_results": params.get("max_results", 60)})
        return parse_arxiv(_get(f"{src['url']}?{q}", **kw).body, src)
    if t == "hn":
        q = urlencode({"tags": "story", "hitsPerPage": 1000,
                       "numericFilters": f"created_at_i>{int(since.timestamp())},points>={params.get('min_points', 100)}"})
        data = json.loads(_get(f"{src['url']}?{q}", **kw).text())
        kw_re = re.compile(params.get("keywords", r"\bAI\b"), re.I)
        data["hits"] = [h for h in data.get("hits", []) if kw_re.search(h.get("title") or "")]
        return parse_hn(data, src, params.get("min_points", 100))
    if t == "hf_papers":
        out, day = [], since.date()
        while day <= now_utc().date():
            try:
                data = json.loads(_get(f"{src['url']}?date={day.isoformat()}", **kw).text())
                out += parse_hf_papers(data, src, params.get("min_upvotes", 10))
            except FetchError:
                pass  # день без выпуска — не ошибка
            day += timedelta(days=1)
        return out
    if t == "hf_models":
        if params.get("authors"):  # новые модели конкретных лабораторий (DeepSeek, Qwen, Meta …)
            out = []
            for author in params["authors"]:
                q = urlencode({"author": author, "sort": "createdAt", "direction": -1,
                               "limit": params.get("limit", 5), "full": "true"})
                rows = json.loads(_get(f"{src['url']}?{q}", **kw).text())
                out += [m for m in parse_hf_models(rows, src) if (parse_dt(m["published_at"]) or since) >= since]
            return out
        q = urlencode({"sort": "trendingScore", "direction": -1, "limit": params.get("limit", 25)})
        return parse_hf_models(json.loads(_get(f"{src['url']}?{q}", **kw).text()), src)
    if t == "bing_news":
        # Поиск новостей по каждой компании watchlist: замена сайтам, которые закрыты для роботов
        # (openai.com, x.ai, anthropic.com/news …). В дайджест всё равно идёт первоисточник — его ищет фактчекер.
        out, failed = [], []
        wl = load_yaml("watchlist.yaml") or {}
        for c in wl.get("companies", []):
            name = c["name"]
            if not re.fullmatch(r"[A-Za-z0-9 .&+\-]+", name):
                continue  # служебные группы на кириллице
            query = c.get("news_query") or f'"{name}" {params.get("suffix", "AI")}'.strip()
            url = f"{src['url']}?{urlencode({'q': query, 'format': 'rss'})}"
            got = None
            for attempt in range(2):  # Bing иногда притормаживает отдельный запрос — повторяем
                try:
                    got = parse_bing_news(_get(url, **kw).body, src, company=name, section=c.get("section"))
                    break
                except Exception:
                    time.sleep(params.get("pause_sec", 1) * 5)
            if got is None:
                failed.append(name)
            else:
                out += [g for g in got if (parse_dt(g["published_at"]) or since) >= since][:params.get("per_company", 8)]
            time.sleep(params.get("pause_sec", 1))
        if failed and not out:
            raise FetchError(f"bing news: все {len(failed)} запросов не удались")
        if failed:
            SOFT_ERRORS.append({"source": f"{src['name']} ({', '.join(failed)})", "error": "поиск по компании не ответил"})
        return out
    if t == "github":
        out = []
        for topic in params.get("topics", ["llm"]):
            q = urlencode({"q": f"topic:{topic} created:>={since.date().isoformat()} "
                                f"stars:>={params.get('min_stars', 200)}",
                           "sort": "stars", "order": "desc", "per_page": 15})
            data = json.loads(_get(f"{src['url']}?{q}", **kw,
                                               headers={"Accept": "application/vnd.github+json"}).text())
            out += parse_github(data, src)
        return out
    raise ValueError(f"Неизвестный тип источника: {t}")


def fetch_telegram(cfg: dict, defaults: dict) -> tuple[list[dict], list[dict]]:
    items, errors = [], []
    limit = cfg.get("max_posts_per_channel", 40)
    for ch in cfg.get("channels", []):
        name = ch["username"].lstrip("@")
        try:
            resp = _get(f"https://t.me/s/{name}", retries=defaults.get("retries", 3),
                                    pause=defaults.get("retry_pause_sec", 5),
                                    timeout=defaults.get("timeout_sec", 20))
            posts = parse_tg(resp.text(), name)
            if not posts:
                raise FetchError(f"t.me/s/{name}: нет постов (канал закрыт для превью?)")
            items += posts[-limit:]
        except Exception as e:
            errors.append({"source": f"t.me/{name}", "error": str(e)})
    return items, errors


def in_window(item: dict, since: datetime, until: datetime) -> bool:
    dt = parse_dt(item.get("published_at"))
    if dt is None:
        return True  # дату проверит фактчекер
    return since <= dt <= until + timedelta(hours=1)


def main(argv=None) -> int:
    setup_stdio()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", required=True)
    ap.add_argument("--group", default="all", choices=GROUPS + ("all",))
    args = ap.parse_args(argv)

    rd = run_dir(args.run)
    init = read_json(rd / "init.json")
    if not init:
        print("Нет init.json — сначала python scripts/pipeline.py init", file=sys.stderr)
        return 2
    log = RunLog(rd)
    since, until = parse_dt(init["window"]["since"]), parse_dt(init["window"]["until"])
    cfg = load_yaml("sources.yaml")
    defaults = cfg.get("defaults", {})
    per_source = defaults.get("max_items_per_source", 40)

    groups = GROUPS if args.group == "all" else (args.group,)
    items, errors, stats = [], [], {}
    for src in cfg.get("feeds", []):
        if src.get("group") not in groups:
            continue
        try:
            got = [i for i in fetch_source(src, since, defaults) if in_window(i, since, until)]
            got = got[:src.get("max_items", per_source)]
            stats[src["name"]] = len(got)
            items += got
        except Exception as e:
            errors.append({"source": src["name"], "error": str(e)[:300]})
            stats[src["name"]] = "error"
    errors += SOFT_ERRORS
    if "telegram" in groups:
        tg_items, tg_errors = fetch_telegram(load_yaml("tg_sources.yaml"), defaults)
        tg_items = [i for i in tg_items if in_window(i, since, until)]
        stats["telegram"] = len(tg_items)
        items += tg_items
        errors += tg_errors

    out = rd / f"candidates_feeds_{args.group}.json"
    write_json(out, {"items": items, "errors": errors, "stats": stats})
    log("collect", f"fetch_feeds[{args.group}]: {len(items)} кандидатов, ошибок источников: {len(errors)}")
    for e in errors:
        log("collect", f"источник недоступен: {e['source']} — {e['error']}")
    print(json.dumps({"out": str(out.relative_to(rd.parent.parent)), "items": len(items),
                      "errors": errors}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
