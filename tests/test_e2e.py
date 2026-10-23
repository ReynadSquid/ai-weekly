"""Сквозной прогон детерминированной части: init → merge → gate → editor-input → split → publish → finalize → report.
LLM-шаги подменены готовыми файлами, Telegram — локальным HTTP-сервером."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import common
import pipeline
import split_messages
import tg_publish

TOKEN = "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKll"
RUN = "2026-10-25_weekly"


@pytest.fixture
def fake_tg():
    received = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, body))
            resp = {"ok": True, "result": {"message_id": 500 + len(received)}}
            data = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", received
    srv.shutdown()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "RUNS", tmp_path / "runs")
    for mod in (pipeline, tg_publish):
        monkeypatch.setattr(mod, "PUBLISHED", tmp_path / "state" / "published.json")
    monkeypatch.setattr(pipeline, "HISTORY", tmp_path / "state" / "history.json")
    monkeypatch.setattr(pipeline, "ARCHIVE", tmp_path / "archive")
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    monkeypatch.setattr(tg_publish, "PAUSE_SEC", 0)
    monkeypatch.setenv("AIW_NOW", "2026-10-25T03:05:00Z")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_TEST_CHAT_ID", "-100123")
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    return tmp_path


def test_full_dry_run(repo, fake_tg, monkeypatch, capsys):
    base, received = fake_tg
    monkeypatch.setenv("TELEGRAM_API_BASE", base)

    pipeline.main(["init", "--dry-run"])
    assert json.loads(capsys.readouterr().out)["status"] == "proceed"
    rd = repo / "runs" / RUN

    # «сборщики»
    items = [{"title": f"News {i} about model release number {i}", "url": f"https://example{i}.com/post/{i}",
              "source_type": "official", "published_at": "2026-10-22T10:00:00Z", "company": "OpenAI",
              "section": "A"} for i in range(16)]
    items.append(dict(items[0], url="https://example0.com/post/0/?utm_source=x"))  # дубль
    common.write_json(rd / "candidates_companies.json", {"items": items, "errors": [{"source": "x.ai", "error": "403"}]})
    pipeline.main(["merge", RUN])
    cands = common.read_json(rd / "candidates.json")["items"]
    assert len(cands) == 16

    # «куратор» и «фактчекер»
    common.write_json(rd / "shortlist.json", {"items": [{"id": c["id"], "importance": 8} for c in cands[:14]],
                                              "reserve": [{"id": c["id"]} for c in cands[14:]]})
    statuses = ["verified"] * 12 + ["rumor", "rejected"]
    common.write_json(rd / "factcheck.json", {"items": [
        {"id": c["id"], "status": s, "primary_source_url": c["url"],
         "checked_claims": [{"claim": "c", "source": c["url"]}], "attribution": "по данным X" if s == "rumor" else None}
        for c, s in zip(cands, statuses)]})
    pipeline.main(["gate", RUN])
    assert common.read_json(rd / "gate.json")["decision"] == "pass"
    pipeline.main(["editor-input", RUN])
    ed = common.read_json(rd / "editor_input.json")
    assert len(ed["items"]) == 13 and ed["issue"]["issue_number"] == 1
    assert all(i["status"] != "rejected" for i in ed["items"])

    # «редактор»
    posts = {"issue_id": RUN, "header": "<b>AI Weekly #1 · 18–25 октября 2026</b>\n\n• a\n• b\n• c",
             "sections": [{"key": "A", "title": "🏢 Лидеры", "items": [
                 {"id": i["id"], "html": f"<b>{i['title']}</b>\n" + "Текст. " * 60 +
                  f"\n<i>Почему важно:</i> так.\n<a href=\"{i['primary_source_url']}\">Источник</a>"}
                 for i in ed["items"] if i["kind"] == "news"]}],
             "footer": "Итог.\n\n#OpenAI #AIWeekly"}
    common.write_json(rd / "posts.json", posts)
    (rd / "draft.md").write_text("# draft", encoding="utf-8")
    assert split_messages.main(["--run", RUN]) == 0
    msgs = common.read_json(rd / "messages.json")["messages"]
    assert len(msgs) >= 2

    # публикация через настоящий HTTP
    assert tg_publish.main(["--run", RUN]) == 0
    assert len(received) == len(msgs)
    assert all(TOKEN in path for path, _ in received)  # токен только в URL запроса к API
    assert received[0][1]["link_preview_options"]["is_disabled"] is False
    assert received[1][1]["link_preview_options"]["is_disabled"] is True

    # повторный запуск того же слота ничего не шлёт
    assert tg_publish.main(["--run", RUN]) == 0
    assert len(received) == len(msgs)
    capsys.readouterr()
    pipeline.main(["init", "--dry-run"])
    assert json.loads(capsys.readouterr().out)["status"] == "already_published"

    pipeline.main(["finalize", RUN])
    assert (repo / "archive" / f"{RUN}.dry-run.md").exists()
    assert common.read_json(pipeline.HISTORY, {"items": []})["items"] == []  # dry-run не трогает историю

    pipeline.main(["report", RUN])
    report = (rd / "report.txt").read_text(encoding="utf-8")
    assert "опубликован" in report and "x.ai" in report
    for f in rd.iterdir():
        assert TOKEN not in f.read_text(encoding="utf-8", errors="ignore")
    assert TOKEN not in (repo / "state" / "published.json").read_text(encoding="utf-8")
