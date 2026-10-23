import json

import pytest

import common
import tg_publish

TOKEN = "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKll"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(tg_publish, "PUBLISHED", tmp_path / "state" / "published.json")
    monkeypatch.setenv("TELEGRAM_TEST_CHAT_ID", "-1001")
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "@mychannel")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    rd = tmp_path / "runs" / "2026-10-25_weekly"
    common.write_json(rd / "init.json", {"mode": "dry-run", "issue_number": 1})
    common.write_json(rd / "messages.json", {"messages": [
        {"index": i, "text": f"msg {i}", "disable_preview": i > 0} for i in range(3)]})
    return tmp_path


class FakeTelegram:
    def __init__(self, script=None):
        self.calls = []
        self.script = list(script or [])
        self.next_id = 100

    def __call__(self, method, payload, *, token):
        self.calls.append((method, payload))
        if self.script:
            r = self.script.pop(0)
            if r is not None:
                return r
        self.next_id += 1
        return {"ok": True, "result": {"message_id": self.next_id}}


def sent_texts(fake):
    return [p["text"] for m, p in fake.calls if m == "sendMessage"]


def test_publish_sends_all_with_pause_and_previews(env):
    fake, sleeps = FakeTelegram(), []
    res = tg_publish.publish("2026-10-25_weekly", token=TOKEN, call=fake, sleep=sleeps.append)
    assert res["status"] == "published"
    assert sent_texts(fake) == ["msg 0", "msg 1", "msg 2"]
    assert [p["link_preview_options"]["is_disabled"] for _, p in fake.calls] == [False, True, True]
    assert all(p["parse_mode"] == "HTML" and p["chat_id"] == "-1001" for _, p in fake.calls)
    assert sleeps == [3, 3]


def test_second_run_publishes_nothing(env):
    tg_publish.publish("2026-10-25_weekly", token=TOKEN, call=FakeTelegram(), sleep=lambda s: None)
    fake = FakeTelegram()
    res = tg_publish.publish("2026-10-25_weekly", token=TOKEN, call=fake, sleep=lambda s: None)
    assert res["status"] == "already_published"
    assert fake.calls == []


def test_429_waits_retry_after(env):
    fake = FakeTelegram([None, {"ok": False, "error_code": 429, "parameters": {"retry_after": 7}}])
    sleeps = []
    res = tg_publish.publish("2026-10-25_weekly", token=TOKEN, call=fake, sleep=sleeps.append)
    assert res["status"] == "published"
    assert 8 in sleeps
    assert sent_texts(fake) == ["msg 0", "msg 1", "msg 1", "msg 2"]


def test_400_stops_and_resume_sends_only_rest(env):
    fake = FakeTelegram([None, {"ok": False, "error_code": 400, "description": "Bad Request: can't parse"}])
    res = tg_publish.publish("2026-10-25_weekly", token=TOKEN, call=fake, sleep=lambda s: None)
    assert res["status"] == "failed" and res["sent"] == 1
    db = json.loads((env / "state" / "published.json").read_text(encoding="utf-8"))
    rec = db["issues"]["2026-10-25_weekly:dry-run"]
    assert rec["status"] == "failed" and len(rec["messages"]) == 1

    fake2 = FakeTelegram()
    res = tg_publish.publish("2026-10-25_weekly", token=TOKEN, call=fake2, sleep=lambda s: None)
    assert res["status"] == "published"
    assert sent_texts(fake2) == ["msg 1", "msg 2"]


def test_changed_message_after_partial_send_is_refused(env):
    fake = FakeTelegram([None, {"ok": False, "error_code": 403, "description": "Forbidden"}])
    tg_publish.publish("2026-10-25_weekly", token=TOKEN, call=fake, sleep=lambda s: None)
    rd = env / "runs" / "2026-10-25_weekly"
    common.write_json(rd / "messages.json", {"messages": [
        {"index": i, "text": f"NEW {i}", "disable_preview": i > 0} for i in range(3)]})
    with pytest.raises(SystemExit):
        tg_publish.publish("2026-10-25_weekly", token=TOKEN, call=FakeTelegram(), sleep=lambda s: None)


def test_live_mode_uses_channel_and_public_link(env):
    rd = env / "runs" / "2026-10-25_weekly"
    common.write_json(rd / "init.json", {"mode": "live"})
    fake = FakeTelegram()
    res = tg_publish.publish("2026-10-25_weekly", token=TOKEN, call=fake, sleep=lambda s: None)
    assert all(p["chat_id"] == "@mychannel" for _, p in fake.calls)
    assert res["link"] == "https://t.me/mychannel/101"


def test_token_never_in_published_or_errors(env):
    fake = FakeTelegram([{"ok": False, "error_code": 401, "description": f"Unauthorized bot{TOKEN}"}])
    tg_publish.publish("2026-10-25_weekly", token=TOKEN, call=fake, sleep=lambda s: None)
    assert TOKEN not in (env / "state" / "published.json").read_text(encoding="utf-8")


def test_mask_hides_token_pattern(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert TOKEN not in common.mask(f"https://api.telegram.org/bot{TOKEN}/sendMessage")
