import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import common
import pipeline

KRAT = ZoneInfo("Asia/Krasnoyarsk")
SCHED = {"slot_grace_hours": 14, "issues": {
    "weekly": {"enabled": True, "day": "sunday", "time": "10:00"},
    "midweek": {"enabled": False, "day": "wednesday", "time": "18:00"}}}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(pipeline, "PUBLISHED", tmp_path / "state" / "published.json")
    monkeypatch.setattr(pipeline, "HISTORY", tmp_path / "state" / "history.json")
    monkeypatch.setattr(pipeline, "ARCHIVE", tmp_path / "archive")
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    return tmp_path


def run_init(capsys, *args):
    pipeline.main(["init", *args])
    return json.loads(capsys.readouterr().out)


# ---------- слоты ----------

def test_slot_matches_sunday_morning():
    now = datetime(2026, 10, 25, 10, 7, tzinfo=KRAT)  # воскресенье
    kind, slot = pipeline.resolve_kind(now, SCHED, "auto")
    assert kind == "weekly" and slot.hour == 10 and slot.date().isoformat() == "2026-10-25"


def test_retry_slot_same_issue_six_hours_later():
    now = datetime(2026, 10, 25, 16, 7, tzinfo=KRAT)
    kind, slot = pipeline.resolve_kind(now, SCHED, "auto")
    assert kind == "weekly" and slot.date().isoformat() == "2026-10-25"


def test_not_due_on_tuesday():
    now = datetime(2026, 10, 27, 12, 0, tzinfo=KRAT)
    assert pipeline.resolve_kind(now, SCHED, "auto") == (None, None)


def test_disabled_midweek_not_due():
    now = datetime(2026, 10, 28, 18, 5, tzinfo=KRAT)
    assert pipeline.resolve_kind(now, SCHED, "auto")[0] is None


# ---------- init / окно / идемпотентность ----------

def test_init_creates_run_with_default_window(repo, capsys, monkeypatch):
    monkeypatch.setenv("AIW_NOW", "2026-10-25T03:05:00Z")  # 10:05 Красноярск
    res = run_init(capsys)
    assert res["status"] == "proceed" and res["run_id"] == "2026-10-25_weekly"
    assert res["mode"] == "dry-run"
    w = res["init"]["window"]
    assert w["since"] == "2026-10-18T03:05:00Z" and w["until"] == "2026-10-25T03:05:00Z"
    assert res["init"]["issue_number"] == 1


def test_window_starts_at_last_live_publication_capped_8_days(repo, capsys, monkeypatch):
    common.write_json(pipeline.PUBLISHED, {"issues": {
        "2026-10-18_weekly:live": {"issue_id": "2026-10-18_weekly", "mode": "live", "status": "published",
                                   "finished_at": "2026-10-18T03:20:00Z"},
        "a:dry-run": {"issue_id": "a", "mode": "dry-run", "status": "published"},
        "b:dry-run": {"issue_id": "b", "mode": "dry-run", "status": "published"}}})
    monkeypatch.setenv("AIW_NOW", "2026-10-25T03:05:00Z")
    res = run_init(capsys, "--live")
    assert res["mode"] == "live"
    assert res["init"]["window"]["since"] == "2026-10-18T03:20:00Z"
    assert res["init"]["issue_number"] == 2

    monkeypatch.setenv("AIW_NOW", "2026-11-08T03:05:00Z")  # две недели спустя
    res = run_init(capsys, "--live")
    assert res["init"]["window"]["since"] == "2026-10-31T03:05:00Z"  # не больше 8 дней


def test_live_blocked_until_two_dry_runs(repo, capsys, monkeypatch):
    monkeypatch.setenv("AIW_NOW", "2026-10-25T03:05:00Z")
    res = run_init(capsys, "--live")
    assert res["mode"] == "dry-run" and res["warnings"]


def test_already_published_slot_is_skipped(repo, capsys, monkeypatch):
    common.write_json(pipeline.PUBLISHED, {"issues": {
        "2026-10-25_weekly:dry-run": {"issue_id": "2026-10-25_weekly", "mode": "dry-run", "status": "published"}}})
    monkeypatch.setenv("AIW_NOW", "2026-10-25T09:05:00Z")  # повторный слот +6 ч
    assert run_init(capsys)["status"] == "already_published"


def test_forced_rerun_of_published_day_gets_new_run_id(repo, capsys, monkeypatch):
    common.write_json(pipeline.PUBLISHED, {"issues": {
        "2026-10-24_weekly:dry-run": {"issue_id": "2026-10-24_weekly", "mode": "dry-run", "status": "published"}}})
    monkeypatch.setenv("AIW_NOW", "2026-10-24T06:36:00Z")  # суббота, вне слота
    assert run_init(capsys, "--dry-run", "--kind", "weekly")["status"] == "already_published"
    res = run_init(capsys, "--dry-run", "--kind", "weekly", "--force")
    assert res["status"] == "proceed" and res["run_id"] == "2026-10-24_weekly-2"


def test_second_init_resumes_and_respects_lock(repo, capsys, monkeypatch):
    monkeypatch.setenv("AIW_NOW", "2026-10-25T03:05:00Z")
    run_init(capsys)
    assert run_init(capsys)["status"] == "locked"
    pipeline.main(["step", "2026-10-25_weekly", "collect"])
    capsys.readouterr()
    (repo / "runs" / "2026-10-25_weekly" / ".lock").unlink()
    res = run_init(capsys)
    assert res["status"] == "resume" and res["next_step"] == "merge" and res["attempts"] == 2


def test_aborted_slot_is_not_rerun(repo, capsys, monkeypatch):
    monkeypatch.setenv("AIW_NOW", "2026-10-25T03:05:00Z")
    run_init(capsys)
    pipeline.main(["step", "2026-10-25_weekly", "gate", "--abort", "мало новостей"])
    capsys.readouterr()
    monkeypatch.setenv("AIW_NOW", "2026-10-25T09:05:00Z")
    assert run_init(capsys)["status"] == "aborted_earlier"


# ---------- дедуп ----------

def cand(url, title, st="media", date="2026-10-22T10:00:00Z"):
    return {"url": url, "title": title, "source_type": st, "published_at": date}


def test_dedup_url_title_history_window():
    since = datetime(2026, 10, 18, tzinfo=timezone.utc)
    until = datetime(2026, 10, 25, tzinfo=timezone.utc)
    items = [
        cand("https://techcrunch.com/x?utm_source=tw", "OpenAI launches GPT-6 with longer context"),
        cand("https://www.techcrunch.com/x/", "dup by url"),
        cand("https://openai.com/index/gpt-6", "OpenAI launches GPT-6 with a longer context", "official"),
        cand("https://old.com/a", "Old news", date="2026-09-01T00:00:00Z"),
        cand("https://seen.com/b", "Already published"),
        cand("https://new.com/c", "Mistral releases a new small model"),
    ]
    history = [{"url": "https://seen.com/b/", "title": "Something"}]
    out, stats = pipeline.dedup(items, history, since, until)
    urls = [o["url"] for o in out]
    assert urls == ["https://openai.com/index/gpt-6", "https://new.com/c"]  # официальный источник главный
    assert stats["out_of_window"] == 1 and stats["in_history"] == 1
    assert stats["dup_url"] + stats["dup_title"] == 2
    assert "https://techcrunch.com/x?utm_source=tw" in out[0]["also_seen_at"]


def test_coerce_importance():
    assert [pipeline.coerce_importance(v) for v in (7, "8", "high", "Medium", "low", 12, "n/a", None)] == \
        [7, 8, 8, 5, 3, 10, None, None]


def test_title_similarity_versions_differ():
    assert pipeline.title_similarity("OpenAI releases GPT-5", "OpenAI releases GPT-6") == 0
    assert pipeline.title_similarity("Gemini 3 is here", "Gemini 3 is here!") > 0.9
    assert pipeline.title_similarity("Apple buys startup", "Mistral raises money") < 0.3


# ---------- пороги ----------

INIT = {"target_items": [12, 20], "min_items": 8, "max_rejected_ratio": 0.4}


def fc(statuses):
    return {"items": [{"id": f"i{n}", "status": s} for n, s in enumerate(statuses)]}


def short(n, reserve=0):
    return {"items": [{"id": f"i{k}"} for k in range(n)], "reserve_left": bool(reserve)}


def test_gate_pass():
    assert pipeline.evaluate_gate(INIT, short(15), fc(["verified"] * 13 + ["rejected"] * 2))["decision"] == "pass"


def test_gate_stop_on_high_reject_ratio():
    r = pipeline.evaluate_gate(INIT, short(15), fc(["verified"] * 8 + ["rejected"] * 7))
    assert r["decision"] == "stop" and "отклонено" in r["reason"]


def test_gate_stop_on_too_few():
    r = pipeline.evaluate_gate(INIT, short(10), fc(["verified"] * 7 + ["rumor"] * 3))
    assert r["decision"] == "stop"


def test_gate_need_reserve_then_pass_with_min():
    assert pipeline.evaluate_gate(INIT, short(12, reserve=5), fc(["verified"] * 10 + ["rejected"] * 2))["decision"] == "need_reserve"
    assert pipeline.evaluate_gate(INIT, short(12), fc(["verified"] * 10 + ["rejected"] * 2))["decision"] == "pass"


def test_gate_need_factcheck_for_unchecked():
    assert pipeline.evaluate_gate(INIT, short(14), fc(["verified"] * 12))["decision"] == "need_factcheck"


# ---------- отчёт и cron ----------

def test_report_masks_token(repo, capsys, monkeypatch):
    token = "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKll"
    monkeypatch.setenv("AIW_NOW", "2026-10-25T03:05:00Z")
    run_init(capsys)
    pipeline.main(["step", "2026-10-25_weekly", "collect", "--error", f"fail https://api.telegram.org/bot{token}/x"])
    capsys.readouterr()
    text = pipeline.build_report("2026-10-25_weekly")
    assert token not in text and "collect" in text
    assert token not in (repo / "runs" / "2026-10-25_weekly" / "run.log").read_text(encoding="utf-8")


def test_step_report_builds_report_if_skipped(repo, capsys, monkeypatch):
    monkeypatch.setenv("AIW_NOW", "2026-10-25T03:05:00Z")
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    run_init(capsys)
    pipeline.main(["step", "2026-10-25_weekly", "report"])
    assert (repo / "runs" / "2026-10-25_weekly" / "report.txt").exists()


def test_cron_converts_to_utc(capsys):
    pipeline.main(["cron"])
    rows = json.loads(capsys.readouterr().out)
    main = next(r for r in rows if r["kind"] == "weekly" and r["purpose"] == "основной слот")
    assert main["cron_local"] == "0 10 * * 0"
    assert main["cron_utc"] == "0 3 * * 0"  # Красноярск = UTC+7


# ---------- понедельник 9:00, окно до полуночи, первый выпуск 02.11 ----------

@pytest.fixture
def monday(fixed_schedule):
    fixed_schedule.update(window_end="day_start", max_window_days=9, first_issue_date="2026-11-02")
    fixed_schedule["issues"]["weekly"].update(day="monday", time="09:00")
    return fixed_schedule


def live(issue_id, until, finished):
    common.write_json(common.RUNS / issue_id / "init.json", {"window": {"until": until}})
    return {"issue_id": issue_id, "mode": "live", "status": "published", "finished_at": finished}


def test_monday_before_first_issue_date_is_not_due(repo, capsys, monkeypatch, monday):
    monkeypatch.setenv("AIW_NOW", "2026-10-26T02:05:00Z")  # пн 26.10 09:05 Красноярск
    assert run_init(capsys)["status"] == "not_due"


def test_first_monday_issue_covers_gap_since_previous_window(repo, capsys, monkeypatch, monday):
    common.write_json(pipeline.PUBLISHED, {"issues": {
        "2026-10-25_weekly:live": live("2026-10-25_weekly", "2026-10-25T03:05:00Z", "2026-10-25T05:47:46Z"),
        "a:dry-run": {"issue_id": "a", "mode": "dry-run", "status": "published"},
        "b:dry-run": {"issue_id": "b", "mode": "dry-run", "status": "published"}}})
    monkeypatch.setenv("AIW_NOW", "2026-11-02T02:05:00Z")  # пн 02.11 09:05
    res = run_init(capsys, "--live")
    assert res["status"] == "proceed" and res["run_id"] == "2026-11-02_weekly"
    assert res["init"]["window"] == {"since": "2026-10-25T03:05:00Z", "until": "2026-11-01T17:00:00Z"}
    assert res["init"]["date_range"] == "25 октября – 1 ноября 2026"
    assert res["init"]["issue_number"] == 2


def test_steady_weekly_window_is_previous_calendar_week(repo, capsys, monkeypatch, monday):
    common.write_json(pipeline.PUBLISHED, {"issues": {
        "2026-11-02_weekly:live": live("2026-11-02_weekly", "2026-11-01T17:00:00Z", "2026-11-02T02:20:00Z"),
        "a:dry-run": {"issue_id": "a", "mode": "dry-run", "status": "published"},
        "b:dry-run": {"issue_id": "b", "mode": "dry-run", "status": "published"}}})
    monkeypatch.setenv("AIW_NOW", "2026-11-09T08:05:00Z")  # повторный слот пн 15:05
    res = run_init(capsys, "--live")
    assert res["init"]["window"] == {"since": "2026-11-01T17:00:00Z", "until": "2026-11-08T17:00:00Z"}
    assert res["init"]["date_range"] == "2–8 ноября 2026"


def test_monday_cron(capsys, monday):
    pipeline.main(["cron"])
    rows = json.loads(capsys.readouterr().out)
    main = next(r for r in rows if r["kind"] == "weekly" and r["purpose"] == "основной слот")
    assert main["cron_local"] == "0 9 * * 1" and main["cron_utc"] == "0 2 * * 1"


def test_report_lists_missed_important(repo, capsys, monkeypatch):
    monkeypatch.setenv("AIW_NOW", "2026-10-25T03:05:00Z")
    run_init(capsys)
    rd = repo / "runs" / "2026-10-25_weekly"
    c = lambda i, imp, t: {"id": i, "title": t, "url": f"https://x.com/{i}", "importance": imp}
    common.write_json(rd / "candidates.json", {"items": [
        c("pub", 9, "Опубликовано"), c("rej", 8, "Отклонено"), c("res", 9, "В резерве"),
        c("skip", 8, "Не отобрано"), c("low", 6, "Неважное"), c("old", 9, "Уже было")]})
    common.write_json(rd / "shortlist.json", {"items": [{"id": "pub"}, {"id": "rej"}],
                                              "reserve": [{"id": "res", "importance": 9}],
                                              "dropped_as_history": ["old"]})
    common.write_json(rd / "factcheck.json", {"items": [{"id": "pub", "status": "verified"},
                                                        {"id": "rej", "status": "rejected"}]})
    common.write_json(rd / "gate.json", {"decision": "pass", "publish_ids": ["pub"], "rumor_ids": []})
    report = pipeline.build_report("2026-10-25_weekly")
    assert "Важное, что не вошло (3):" in report
    assert "[9] В резерве — в резерве куратора" in report
    assert "[8] Отклонено — отклонена фактчекером" in report
    assert "[8] Не отобрано — не отобрана куратором" in report
    assert "Опубликовано" not in report and "Неважное" not in report and "Уже было" not in report


def test_dry_run_on_live_published_day_gets_own_run(repo, capsys, monkeypatch):
    common.write_json(pipeline.PUBLISHED, {"issues": {
        "2026-10-25_weekly:live": {"issue_id": "2026-10-25_weekly", "mode": "live", "status": "published"}}})
    common.write_json(common.RUNS / "2026-10-25_weekly" / "init.json", {"mode": "live", "run_id": "2026-10-25_weekly"})
    monkeypatch.setenv("AIW_NOW", "2026-10-25T07:30:00Z")
    res = run_init(capsys, "--dry-run", "--kind", "weekly", "--force", "--since-days", "7")
    assert res["status"] == "proceed" and res["run_id"] == "2026-10-25_weekly-2"
    assert res["init"]["window"]["since"] == "2026-10-18T07:30:00Z"
    assert common.read_json(common.RUNS / "2026-10-25_weekly" / "init.json")["mode"] == "live"  # боевой не тронут
