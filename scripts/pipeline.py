"""Детерминированные шаги пайплайна. Оркестратор (главная сессия Claude Code) вызывает их по очереди.

    python scripts/pipeline.py init [--kind auto|weekly|midweek] [--dry-run|--live] [--resume] [--force]
    python scripts/pipeline.py step <run_id> <step> [--note TEXT]    # отметить шаг выполненным
    python scripts/pipeline.py merge <run_id>                        # candidates_*.json → candidates.json
    python scripts/pipeline.py gate <run_id>                         # пороги после фактчека
    python scripts/pipeline.py editor-input <run_id>                 # только проверенные новости для редактора
    python scripts/pipeline.py finalize <run_id>                     # history.json, archive/
    python scripts/pipeline.py report <run_id> [--send] [--event TEXT]
    python scripts/pipeline.py checkpoint <run_id> [-m MSG]          # git commit + push состояния
    python scripts/pipeline.py cron                                  # cron-строки из schedule.yaml
    python scripts/pipeline.py status <run_id>

Все команды печатают JSON в stdout; подробности — в runs/<id>/run.log.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import (ARCHIVE, ROOT, RUNS, STATE, RunLog, iso, load_dotenv, load_yaml, mask,
                    normalize_url, now_utc, parse_dt, read_json, run_dir, setup_stdio, tz, url_id,
                    write_json)

STEPS = ["init", "collect", "merge", "rank", "factcheck", "gate", "write", "qa", "publish",
         "finalize", "report"]
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
          "октября", "ноября", "декабря"]
HISTORY = STATE / "history.json"
PUBLISHED = STATE / "published.json"
LOCK_TTL = timedelta(hours=3)


def out(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ---------- расписание и слоты ----------

def slot_for(now_local: datetime, sched: dict, kind: str) -> datetime | None:
    """Последний слот выпуска kind, который начался не позже now и не раньше now - grace."""
    cfg = sched["issues"][kind]
    hh, mm = map(int, str(cfg["time"]).split(":"))
    target_wd = DAYS.index(cfg["day"].lower())
    days_back = (now_local.weekday() - target_wd) % 7
    slot = (now_local - timedelta(days=days_back)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    if slot > now_local:
        slot -= timedelta(days=7)
    grace = timedelta(hours=sched.get("slot_grace_hours", 14))
    return slot if now_local - slot <= grace else None


def resolve_kind(now_local: datetime, sched: dict, kind: str) -> tuple[str | None, datetime | None]:
    if kind != "auto":
        return kind, slot_for(now_local, sched, kind)
    for k, cfg in sched["issues"].items():
        if cfg.get("enabled"):
            slot = slot_for(now_local, sched, k)
            if slot:
                return k, slot
    return None, None


def live_issues(db: dict) -> list[dict]:
    return [r for r in db.get("issues", {}).values() if r.get("mode") == "live" and r.get("status") == "published"]


def date_range_label(since: datetime, until: datetime, tzinfo) -> str:
    a, b = since.astimezone(tzinfo), until.astimezone(tzinfo)
    if a.month == b.month:
        return f"{a.day}–{b.day} {MONTHS[b.month - 1]} {b.year}"
    return f"{a.day} {MONTHS[a.month - 1]} – {b.day} {MONTHS[b.month - 1]} {b.year}"


def cmd_init(args) -> int:
    sched = load_yaml("schedule.yaml")
    tzinfo = tz()
    now = now_utc()
    now_local = now.astimezone(tzinfo)

    kind, slot = resolve_kind(now_local, sched, args.kind)
    if kind is None:
        if not args.force:
            out({"status": "not_due", "now_local": now_local.isoformat(),
                 "hint": "сейчас нет слота выпуска; для ручного запуска укажите --kind weekly"})
            return 0
        kind = "weekly"
    if kind not in sched["issues"]:
        out({"status": "error", "error": f"неизвестный тип выпуска {kind}"})
        return 2
    slot_date = (slot or now_local).date().isoformat()
    run_id = f"{slot_date}_{kind}"
    rd = run_dir(run_id)

    db = read_json(PUBLISHED, {"issues": {}})
    dry_ok = sum(1 for r in db["issues"].values() if r.get("mode") == "dry-run" and r.get("status") == "published")
    mode = sched.get("publish_mode", "dry-run")
    if args.dry_run:
        mode = "dry-run"
    elif args.live:
        mode = "live"
    warnings = []
    need = int(sched.get("min_dry_runs_before_live", 2))
    if mode == "live" and dry_ok < need and not live_issues(db):
        warnings.append(f"live запрещён: успешных dry-run выпусков {dry_ok} из {need}; выпуск пойдёт в dry-run")
        mode = "dry-run"

    key = f"{run_id}:{mode}"
    rec = db["issues"].get(key)
    if rec and rec.get("status") == "published" and not args.force:
        out({"status": "already_published", "run_id": run_id, "mode": mode})
        return 0

    existing = read_json(rd / "init.json")
    progress = read_json(rd / "progress.json", {"done": []})
    # Повторный запуск того же слота продолжает с места остановки (--resume неявно),
    # если только явно не просили начать заново (--fresh).
    if existing and not args.fresh:
        if existing.get("mode") != mode:
            warnings.append(f"режим запуска изменён на {mode}")
            existing["mode"] = mode
            write_json(rd / "init.json", existing)
        if progress.get("aborted") and not args.force:
            out({"status": "aborted_earlier", "run_id": run_id, "reason": progress["aborted"],
                 "hint": "выпуск этого слота уже остановлен порогами; --force --fresh чтобы собрать заново"})
            return 0
        lock = _take_lock(rd, args.force)
        if lock:
            out(lock)
            return 0
        attempts = progress.get("attempts", 0) + 1
        progress["attempts"] = attempts
        write_json(rd / "progress.json", progress)
        RunLog(rd)("init", f"продолжение запуска (попытка {attempts}), выполнено: {progress['done']}")
        out({"status": "resume", "run_id": run_id, "run_dir": str(rd.relative_to(ROOT)), "mode": mode,
             "done": progress["done"], "next_step": next_step(progress), "attempts": attempts,
             "init": existing, "warnings": warnings})
        return 0

    if rd.exists() and args.fresh:
        for f in rd.glob("*"):
            if f.is_file():
                f.unlink()
    rd.mkdir(parents=True, exist_ok=True)
    lock = _take_lock(rd, args.force)
    if lock:
        out(lock)
        return 0

    max_days = sched.get("max_window_days", 8)
    lives = live_issues(db)
    last = max((parse_dt(r.get("finished_at")) for r in lives), default=None)
    since = last if last else now - timedelta(days=sched.get("default_window_days", 7))
    since = max(since, now - timedelta(days=max_days))
    issue_number = len({r["issue_id"] for r in lives if r["issue_id"].endswith(f"_{kind}")}) + 1
    cfg = sched["issues"][kind]
    init = {
        "run_id": run_id, "kind": kind, "mode": mode, "title": cfg.get("title", "AI Weekly"),
        "issue_number": issue_number, "slot_local": slot.isoformat() if slot else None,
        "started_at": iso(now),
        "window": {"since": iso(since), "until": iso(now)},
        "date_range": date_range_label(since, now, tzinfo),
        "target_items": cfg.get("target_items", [12, 20]), "min_items": cfg.get("min_items", 8),
        "max_rejected_ratio": sched.get("max_rejected_ratio", 0.4),
    }
    write_json(rd / "init.json", init)
    write_json(rd / "progress.json", {"done": ["init"], "attempts": 1, "errors": []})
    log = RunLog(rd)
    log("init", f"run={run_id} mode={mode} окно {init['window']['since']} → {init['window']['until']}")
    for w in warnings:
        log("init", w)
    out({"status": "proceed", "run_id": run_id, "run_dir": str(rd.relative_to(ROOT)), "mode": mode,
         "next_step": "collect", "init": init, "warnings": warnings})
    return 0


def _take_lock(rd: Path, force: bool) -> dict | None:
    lock = rd / ".lock"
    if lock.exists() and not force:
        info = read_json(lock, {})
        started = parse_dt(info.get("at"))
        if started and now_utc() - started < LOCK_TTL:
            return {"status": "locked", "run_id": rd.name, "since": info.get("at"),
                    "hint": "другой запуск этого слота ещё идёт; --force чтобы снять lock"}
    write_json(lock, {"at": iso(now_utc()), "pid": os.getpid()})
    return None


def next_step(progress: dict) -> str | None:
    for s in STEPS:
        if s not in progress.get("done", []):
            return s
    return None


def cmd_step(args) -> int:
    rd = run_dir(args.run_id)
    progress = read_json(rd / "progress.json", {"done": [], "errors": []})
    if args.error:
        progress.setdefault("errors", []).append({"step": args.step, "error": mask(args.error),
                                                  "at": iso(now_utc())})
        RunLog(rd)(args.step, f"ОШИБКА: {args.error}")
    else:
        if args.step not in progress["done"]:
            progress["done"].append(args.step)
        RunLog(rd)(args.step, f"шаг выполнен. {args.note or ''}".strip())
    if args.abort:
        progress["aborted"] = args.abort
        RunLog(rd)(args.step, f"ВЫПУСК ОСТАНОВЛЕН: {args.abort}")
    write_json(rd / "progress.json", progress)
    if args.step in ("report",) or args.abort:
        (rd / ".lock").unlink(missing_ok=True)
    out({"run_id": args.run_id, "done": progress["done"], "next_step": next_step(progress)})
    return 0


def cmd_status(args) -> int:
    rd = run_dir(args.run_id)
    progress = read_json(rd / "progress.json", {})
    files = sorted(p.name for p in rd.glob("*.json"))
    out({"run_id": args.run_id, "progress": progress, "next_step": next_step(progress), "files": files})
    return 0


# ---------- merge / dedup ----------

def title_similarity(a: str, b: str) -> float:
    """Похожесть заголовков 0..1. Разные числа/версии (GPT-5 vs GPT-6) — всегда разные новости.
    Смысловые дубли с разными формулировками склеивает куратор."""
    a, b = (a or "").lower(), (b or "").lower()
    if set(re.findall(r"\d+(?:\.\d+)*", a)) != set(re.findall(r"\d+(?:\.\d+)*", b)):
        return 0.0
    wa, wb = set(re.findall(r"[^\W\d_]{2,}", a)), set(re.findall(r"[^\W\d_]{2,}", b))
    if not wa or not wb:
        return 0.0
    jacc = len(wa & wb) / len(wa | wb)
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    return jacc if seq < 0.9 else max(jacc, seq)


SOURCE_RANK = {"official": 0, "research": 1, "media": 2, "aggregator": 3, "telegram": 4}


def load_candidate_files(rd: Path) -> tuple[list[dict], list[dict]]:
    items, errors = [], []
    for f in sorted(rd.glob("candidates_*.json")):
        try:
            data = read_json(f)
        except json.JSONDecodeError as e:
            errors.append({"source": f.name, "error": f"битый JSON: {e}"})
            continue
        rows = data.get("items", data) if isinstance(data, dict) else data
        if isinstance(data, dict):
            errors += data.get("errors", [])
        for r in rows or []:
            if isinstance(r, dict) and r.get("url") and r.get("title"):
                r.setdefault("found_by", f.stem.replace("candidates_", ""))
                items.append(r)
    return items, errors


def history_items() -> list[dict]:
    return read_json(HISTORY, {"items": []}).get("items", [])


def dedup(items: list[dict], history: list[dict], since: datetime, until: datetime,
          sim_threshold: float = 0.72) -> tuple[list[dict], dict]:
    """Склейка дублей по URL и похожести заголовка, отсев истории и событий вне окна."""
    stats = {"input": len(items), "dup_url": 0, "dup_title": 0, "in_history": 0, "out_of_window": 0}
    hist_urls = {normalize_url(h["url"]) for h in history}
    hist_urls |= {normalize_url(u) for h in history for u in h.get("urls", [])}
    hist_titles = [h.get("title", "") for h in history]

    clusters: list[dict] = []
    by_url: dict[str, dict] = {}
    for it in sorted(items, key=lambda x: SOURCE_RANK.get(x.get("source_type"), 5)):
        it = dict(it)
        it["url"] = it["url"].strip()
        nurl = normalize_url(it["url"])
        it["id"] = url_id(it["url"])
        dt = parse_dt(it.get("published_at"))
        if dt and (dt < since - timedelta(hours=12) or dt > until + timedelta(hours=2)):
            stats["out_of_window"] += 1
            continue
        if nurl in hist_urls or any(title_similarity(it["title"], t) >= 0.85 for t in hist_titles):
            stats["in_history"] += 1
            continue
        if nurl in by_url:
            stats["dup_url"] += 1
            _absorb(by_url[nurl], it)
            continue
        match = next((c for c in clusters if title_similarity(c["title"], it["title"]) >= sim_threshold), None)
        if match:
            stats["dup_title"] += 1
            _absorb(match, it)
            by_url[nurl] = match
            continue
        it.setdefault("also_seen_at", [])
        clusters.append(it)
        by_url[nurl] = it
    stats["output"] = len(clusters)
    return clusters, stats


def _absorb(main: dict, dup: dict) -> None:
    seen = main.setdefault("also_seen_at", [])
    for u in [dup["url"]] + ([dup["discovered_via"]] if dup.get("discovered_via") else []):
        if u != main["url"] and u not in seen:
            seen.append(u)
    for k in ("company", "product", "section", "summary_raw", "published_at"):
        if not main.get(k) and dup.get(k):
            main[k] = dup[k]
    if (dup.get("importance") or 0) > (main.get("importance") or 0):
        main["importance"] = dup["importance"]


def cmd_merge(args) -> int:
    rd = run_dir(args.run_id)
    init = read_json(rd / "init.json")
    log = RunLog(rd)
    items, errors = load_candidate_files(rd)
    since, until = parse_dt(init["window"]["since"]), parse_dt(init["window"]["until"])
    merged, stats = dedup(items, history_items(), since, until)
    watch = {c["name"].lower() for c in load_yaml("watchlist.yaml").get("companies", [])}
    for m in merged:
        m["in_watchlist"] = bool(m.get("company") and m["company"].lower() in watch)
    write_json(rd / "candidates.json", {"items": merged, "source_errors": errors, "stats": stats})
    log("merge", f"вход {stats['input']} → выход {stats['output']} "
                 f"(дубли url {stats['dup_url']}, дубли заголовков {stats['dup_title']}, "
                 f"история {stats['in_history']}, вне окна {stats['out_of_window']}); "
                 f"ошибок источников {len(errors)}")
    if stats["output"] < 30:
        log("merge", "ПРЕДУПРЕЖДЕНИЕ: кандидатов меньше 30 — сбор, вероятно, неполный")
    out({"run_id": args.run_id, "stats": stats, "source_errors": len(errors)})
    return 0


# ---------- пороги после фактчека ----------

def evaluate_gate(init: dict, shortlist: dict, factcheck: dict) -> dict:
    items = {i["id"]: i for i in factcheck.get("items", [])}
    picked = [i["id"] for i in shortlist.get("items", [])]
    checked = [items[i] for i in picked if i in items]
    statuses = [c["status"] for c in checked]
    publishable = [c for c in checked if c["status"] in ("verified", "partially")]
    rumors = [c for c in checked if c["status"] == "rumor"]
    rejected = [c for c in checked if c["status"] == "rejected"]
    reserve_checked = [c for i, c in items.items() if i not in picked]
    publishable += [c for c in reserve_checked if c["status"] in ("verified", "partially")]
    total_checked = len(checked) + len(reserve_checked)
    rejected_all = len(rejected) + sum(1 for c in reserve_checked if c["status"] == "rejected")
    ratio = rejected_all / total_checked if total_checked else 1.0
    lo, hi = init.get("target_items", [12, 20])
    result = {
        "checked": total_checked, "publishable": len(publishable), "rumor": len(rumors),
        "rejected": rejected_all, "rejected_ratio": round(ratio, 3),
        "unchecked_in_shortlist": [i for i in picked if i not in items],
        "statuses": {s: statuses.count(s) for s in set(statuses)},
    }
    if result["unchecked_in_shortlist"]:
        result["decision"] = "need_factcheck"
    elif ratio > init.get("max_rejected_ratio", 0.4):
        result["decision"] = "stop"
        result["reason"] = f"отклонено {ratio:.0%} проверенного (> {init.get('max_rejected_ratio', 0.4):.0%}) — сбор, похоже, сломан"
    elif len(publishable) >= lo:
        result["decision"] = "pass"
    elif len(publishable) < init.get("min_items", 8) and not shortlist.get("reserve_left"):
        result["decision"] = "stop"
        result["reason"] = f"после фактчека осталось {len(publishable)} новостей (< {init.get('min_items', 8)})"
    elif len(publishable) >= init.get("min_items", 8) and not shortlist.get("reserve_left"):
        result["decision"] = "pass"
    else:
        result["decision"] = "need_reserve"
        result["need"] = lo - len(publishable)
    result["publish_ids"] = [c["id"] for c in publishable][:hi]
    result["rumor_ids"] = [c["id"] for c in rumors][:3]
    return result


def cmd_gate(args) -> int:
    rd = run_dir(args.run_id)
    init = read_json(rd / "init.json")
    shortlist = read_json(rd / "shortlist.json", {})
    fc = read_json(rd / "factcheck.json", {})
    shortlist["reserve_left"] = bool(
        [r for r in shortlist.get("reserve", []) if r["id"] not in {i["id"] for i in fc.get("items", [])}])
    result = evaluate_gate(init, shortlist, fc)
    write_json(rd / "gate.json", result)
    RunLog(rd)("gate", f"решение={result['decision']} годных={result['publishable']} "
                       f"слухов={result['rumor']} отклонено={result['rejected']} "
                       f"({result['rejected_ratio']:.0%}) {result.get('reason', '')}")
    out(result)
    return 0


def cmd_editor_input(args) -> int:
    """Редактор получает только то, что прошло фактчек, с подтверждёнными утверждениями."""
    rd = run_dir(args.run_id)
    gate = read_json(rd / "gate.json")
    if not gate or gate.get("decision") != "pass":
        out({"error": "gate.json отсутствует или decision != pass"})
        return 1
    cands = {c["id"]: c for c in read_json(rd / "candidates.json", {}).get("items", [])}
    short = read_json(rd / "shortlist.json", {})
    for c in short.get("items", []) + short.get("reserve", []):
        cands.setdefault(c["id"], c)
        cands[c["id"]].update({k: v for k, v in c.items() if v not in (None, "")})
    fc = {f["id"]: f for f in read_json(rd / "factcheck.json", {}).get("items", [])}
    watch = load_yaml("watchlist.yaml").get("companies", [])
    tags = {c["name"].lower(): c.get("hashtag") for c in watch}
    rows = []
    for kind, ids in (("news", gate["publish_ids"]), ("rumor", gate.get("rumor_ids", []))):
        for i in ids:
            c, f = cands.get(i, {}), fc[i]
            rows.append({
                "id": i, "kind": kind, "status": f["status"], "title": c.get("title"),
                "company": c.get("company"), "product": c.get("product"),
                "section": "G" if kind == "rumor" else c.get("section"),
                "importance": c.get("importance"), "published_at": c.get("published_at"),
                "summary_raw": c.get("summary_raw"),
                "primary_source_url": f.get("primary_source_url"),
                "attribution": f.get("attribution"),
                "checked_claims": f.get("checked_claims", []),
                "removed_claims": f.get("removed_claims", []),
                "hashtag": tags.get((c.get("company") or "").lower()),
            })
    init = read_json(rd / "init.json")
    write_json(rd / "editor_input.json", {
        "issue": {k: init[k] for k in ("run_id", "kind", "title", "issue_number", "date_range", "mode")},
        "items": rows,
    })
    RunLog(rd)("write", f"editor_input: {len(rows)} новостей")
    out({"items": len(rows)})
    return 0


# ---------- финализация и отчёт ----------

def cmd_finalize(args) -> int:
    rd = run_dir(args.run_id)
    init = read_json(rd / "init.json")
    db = read_json(PUBLISHED, {"issues": {}})
    rec = db["issues"].get(f"{args.run_id}:{init['mode']}")
    if not rec or rec.get("status") != "published":
        out({"error": "выпуск не опубликован — finalize не выполняется"})
        return 1
    draft = rd / "draft.md"
    if draft.exists():
        ARCHIVE.mkdir(exist_ok=True)
        name = args.run_id if init["mode"] == "live" else f"{args.run_id}.dry-run"
        (ARCHIVE / f"{name}.md").write_text(draft.read_text(encoding="utf-8"), encoding="utf-8")
    added = 0
    if init["mode"] == "live":  # dry-run не должен «съедать» новости будущих выпусков
        ed = read_json(rd / "editor_input.json", {"items": []})
        cands = {c["id"]: c for c in read_json(rd / "candidates.json", {}).get("items", [])}
        hist = read_json(HISTORY, {"items": []})
        known = {h["id"] for h in hist["items"]}
        for it in ed["items"]:
            if it["id"] in known:
                continue
            c = cands.get(it["id"], {})
            hist["items"].append({
                "id": it["id"], "title": it["title"], "url": it["primary_source_url"] or c.get("url"),
                "urls": [u for u in [c.get("url")] + c.get("also_seen_at", []) if u],
                "company": it.get("company"), "issue_id": args.run_id, "published_at": iso(now_utc()),
            })
            added += 1
        weeks = load_yaml("schedule.yaml").get("history_weeks", 8)
        cutoff = now_utc() - timedelta(weeks=weeks)
        hist["items"] = [h for h in hist["items"] if (parse_dt(h["published_at"]) or now_utc()) >= cutoff]
        write_json(HISTORY, hist)
    RunLog(rd)("finalize", f"архив сохранён, в историю добавлено {added}")
    out({"archived": True, "history_added": added})
    return 0


def build_report(run_id: str, event: str | None = None) -> str:
    rd = run_dir(run_id)
    init = read_json(rd / "init.json", {})
    progress = read_json(rd / "progress.json", {})
    cand = read_json(rd / "candidates.json", {})
    gate = read_json(rd / "gate.json", {})
    qa = read_json(rd / "qa_report.json", {})
    db = read_json(PUBLISHED, {"issues": {}})
    rec = db["issues"].get(f"{run_id}:{init.get('mode')}", {})
    stats = cand.get("stats", {})
    ok = rec.get("status") == "published"
    head = "✅ Выпуск опубликован" if ok else ("⛔ Выпуск НЕ опубликован" if progress.get("aborted") or
                                             rec.get("status") == "failed" else "⚠️ Выпуск не завершён")
    lines = [f"{head} — {init.get('title', 'AI Weekly')} #{init.get('issue_number', '?')} "
             f"({run_id}, режим {init.get('mode')})"]
    if event:
        lines.append(f"Событие: {event}")
    lines.append(f"Окно: {init.get('date_range', '?')}")
    lines.append(f"Собрано: {stats.get('input', '?')} → после дедупа {stats.get('output', '?')}")
    if gate:
        lines.append(f"Фактчек: проверено {gate.get('checked')}, годных {gate.get('publishable')}, "
                     f"слухов {gate.get('rumor')}, отклонено {gate.get('rejected')} "
                     f"({gate.get('rejected_ratio', 0):.0%})")
        if gate.get("publish_ids"):
            lines.append(f"В выпуске: {len(gate['publish_ids'])} новостей + {len(gate.get('rumor_ids', []))} слухов")
    if qa:
        lines.append(f"QA: {qa.get('status')}" + (f", замечаний {len(qa.get('issues', []))}" if qa.get("issues") else ""))
    if rec:
        lines.append(f"Telegram: {rec.get('status')}, сообщений {len(rec.get('messages', []))}"
                     + (f", ссылка {rec['link']}" if rec.get("link") else ""))
        if rec.get("error"):
            lines.append(f"Ошибка Telegram: {rec['error']}")
    if progress.get("aborted"):
        lines.append(f"Причина остановки: {progress['aborted']}")
    src_err = cand.get("source_errors", [])
    if src_err:
        lines.append(f"Недоступные источники ({len(src_err)}): " +
                     "; ".join(e.get("source", "?") for e in src_err[:12]))
    for e in progress.get("errors", [])[-8:]:
        lines.append(f"Ошибка шага {e['step']}: {e['error'][:200]}")
    lines.append(f"Попыток запуска: {progress.get('attempts', 1)}. Лог: runs/{run_id}/run.log")
    return mask("\n".join(lines))


def cmd_report(args) -> int:
    load_dotenv()
    rd = run_dir(args.run_id)
    text = build_report(args.run_id, args.event)
    (rd / "report.txt").write_text(text, encoding="utf-8")
    sent = False
    if args.send:
        from tg_publish import TelegramError, send_report
        try:
            sent = send_report(text, token=os.environ.get("TELEGRAM_BOT_TOKEN"))
        except TelegramError as e:
            RunLog(rd)("report", f"отчёт админу не отправлен: {e}")
    RunLog(rd)("report", f"отчёт сформирован, отправлен админу: {sent}")
    print(text)
    return 0


# ---------- git-чекпоинт ----------

def cmd_checkpoint(args) -> int:
    def git(*a, check=True):
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=check)

    try:
        git("rev-parse", "--is-inside-work-tree")
    except Exception:
        out({"skipped": "не git-репозиторий"})
        return 0
    paths = ["state", "archive", f"runs/{args.run_id}"]
    git("add", "--", *[p for p in paths if (ROOT / p).exists()])
    if not git("diff", "--cached", "--quiet", check=False).returncode:
        out({"committed": False, "reason": "нет изменений"})
        return 0
    msg = args.message or f"chore(state): {args.run_id}"
    git("-c", "user.name=AI Weekly Bot", "-c", "user.email=ai-weekly-bot@users.noreply.github.com",
        "commit", "-m", msg)
    if not git("remote", check=False).stdout.strip():
        out({"committed": True, "pushed": False, "reason": "нет remote"})
        return 0
    # Состояние всегда пушится в ветку по умолчанию (main): облачный запуск может стоять
    # на рабочей ветке claude/*, а следующий запуск клонирует именно main.
    branch = os.environ.get("AIW_STATE_BRANCH", "main")
    for attempt in range(3):
        push = git("push", "origin", f"HEAD:{branch}", check=False)
        if push.returncode == 0:
            out({"committed": True, "pushed": True, "branch": branch})
            return 0
        git("pull", "--rebase", "origin", branch, check=False)
    out({"committed": True, "pushed": False, "error": mask(push.stderr[-500:])})
    return 1


def cmd_cron(args) -> int:
    sched = load_yaml("schedule.yaml")
    tzinfo = tz()
    rows = []
    for kind, cfg in sched["issues"].items():
        hh, mm = map(int, str(cfg["time"]).split(":"))
        wd = DAYS.index(cfg["day"].lower())
        offsets = [0] + list(sched.get("retry_after_hours", []))
        for off in offsets:
            ref = datetime(2026, 1, 5, hh, mm, tzinfo=tzinfo) + timedelta(days=wd, hours=off)
            utc = ref.astimezone(timezone.utc)
            cron_wd = (utc.weekday() + 1) % 7  # cron: 0 = воскресенье
            local_wd = (ref.weekday() + 1) % 7
            rows.append({"kind": kind, "enabled": cfg.get("enabled", False),
                         "purpose": "основной слот" if off == 0 else f"повтор через {off} ч",
                         "local": f"{ref.strftime('%A %H:%M')} {sched.get('timezone')}",
                         "cron_local": f"{ref.minute} {ref.hour} * * {local_wd}",
                         "cron_utc": f"{utc.minute} {utc.hour} * * {cron_wd}"})
    out(rows)
    return 0


def main(argv=None) -> int:
    setup_stdio()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--kind", default="auto", choices=["auto", "weekly", "midweek"])
    m = p.add_mutually_exclusive_group()
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--live", action="store_true")
    p.add_argument("--resume", action="store_true", help="продолжить (по умолчанию и так продолжает)")
    p.add_argument("--fresh", action="store_true", help="начать слот заново, удалив промежуточные файлы")
    p.add_argument("--force", action="store_true", help="игнорировать слот/lock/«уже опубликовано»")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("step")
    p.add_argument("run_id")
    p.add_argument("step", choices=STEPS)
    p.add_argument("--note")
    p.add_argument("--error")
    p.add_argument("--abort", help="остановить выпуск с указанной причиной")
    p.set_defaults(fn=cmd_step)
    for name, fn in (("merge", cmd_merge), ("gate", cmd_gate), ("editor-input", cmd_editor_input),
                     ("finalize", cmd_finalize), ("status", cmd_status)):
        p = sub.add_parser(name)
        p.add_argument("run_id")
        p.set_defaults(fn=fn)
    p = sub.add_parser("report")
    p.add_argument("run_id")
    p.add_argument("--send", action="store_true")
    p.add_argument("--event")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("checkpoint")
    p.add_argument("run_id")
    p.add_argument("-m", "--message")
    p.set_defaults(fn=cmd_checkpoint)
    p = sub.add_parser("cron")
    p.set_defaults(fn=cmd_cron)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
