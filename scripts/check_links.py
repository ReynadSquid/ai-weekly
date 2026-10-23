"""Проверка ссылок: отвечает ли URL 200 и не уводит ли редирект на главную.

Использование:
    python scripts/check_links.py https://a.example/x https://b.example/y
    python scripts/check_links.py --run 2026-10-25_weekly --from shortlist   # url всех отобранных
    python scripts/check_links.py --run 2026-10-25_weekly --from posts       # ссылки из posts.json

Вердикты:
    ok            — HTTP 200 и итоговая страница не главная
    homepage      — редирект увёл на главную/корень сайта (ссылка «битая по смыслу»)
    unverifiable  — 401/403/429 от сайта с антибот-защитой (config/sources.yaml → link_check)
    broken        — всё остальное (4xx/5xx, таймаут, DNS)
С --run результат пишется в runs/<id>/links_<from>.json. Код выхода 1, если есть broken/homepage.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from common import domain, load_yaml, read_json, run_dir, setup_stdio, write_json
from net import http_get

HREF_RE = re.compile(r'href="([^"]+)"')


def classify(url: str, status: int | None, final_url: str | None, protected: list[str]) -> str:
    if status == 200:
        orig, fin = urlsplit(url), urlsplit(final_url or url)
        orig_deep = orig.path.strip("/") != ""
        fin_root = fin.path.strip("/") in ("", "en", "en-us", "ru", "index.html")
        if orig_deep and fin_root and not fin.query:
            return "homepage"
        return "ok"
    if status in (401, 403, 429, 503) and any(domain(url).endswith(d) for d in protected):
        return "unverifiable"
    return "broken"


def check_one(url: str, protected: list[str], retries: int = 3) -> dict:
    status, final, error = None, None, None
    for _ in range(retries):
        try:
            resp = http_get(url, timeout=20, max_bytes=200_000)
            status, final = resp.status, resp.url
            if status < 500 and status != 429:
                break
        except Exception as e:
            error = f"{type(e).__name__}: {e}"[:200]
    return {"url": url, "status": status, "final_url": final, "error": error,
            "verdict": classify(url, status, final, protected)}


def check_many(urls: list[str], protected: list[str] | None = None) -> dict[str, dict]:
    if protected is None:
        protected = (load_yaml("sources.yaml").get("link_check") or {}).get("bot_protected_domains", [])
    uniq = list(dict.fromkeys(u for u in urls if u))
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda u: check_one(u, protected), uniq))
    return {r["url"]: r for r in results}


def urls_from_run(run_id: str, source: str) -> list[str]:
    rd = run_dir(run_id)
    if source == "shortlist":
        data = read_json(rd / "shortlist.json", {})
        return [i["url"] for i in data.get("items", []) + data.get("reserve", [])]
    if source == "factcheck":
        data = read_json(rd / "factcheck.json", {})
        return [i["primary_source_url"] for i in data.get("items", []) if i.get("primary_source_url")]
    if source == "posts":
        text = json.dumps(read_json(rd / "posts.json", {}), ensure_ascii=False)
        return [u.replace("&amp;", "&") for u in HREF_RE.findall(text.replace('\\"', '"'))]
    raise SystemExit(f"неизвестный источник {source}")


def main(argv=None) -> int:
    setup_stdio()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--run")
    ap.add_argument("--from", dest="source", choices=("shortlist", "factcheck", "posts"))
    args = ap.parse_args(argv)

    urls = list(args.urls)
    if args.run and args.source:
        urls += urls_from_run(args.run, args.source)
    results = check_many(urls)
    if args.run and args.source:
        write_json(run_dir(args.run) / f"links_{args.source}.json", results)
    bad = [r for r in results.values() if r["verdict"] in ("broken", "homepage")]
    summary = {"checked": len(results), "bad": len(bad),
               "unverifiable": sum(r["verdict"] == "unverifiable" for r in results.values()),
               "bad_urls": [{"url": r["url"], "status": r["status"], "verdict": r["verdict"],
                             "error": r["error"]} for r in bad]}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
