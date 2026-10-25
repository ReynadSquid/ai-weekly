import json

import pytest

import common
import make_cover
import split_messages
import tg_publish

TOKEN = "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKll"
RUN = "2026-10-25_weekly"
MAPPING = {"openai": "openai", "anthropic": "anthropic", "moonshotai": "kimi"}


# ---------- плитки ----------

def test_plan_tiles_orders_and_collapses_rest():
    companies = [("OpenAI", 3), ("Anthropic", 2), ("Moonshot AI", 1), ("Canva", 1), ("Block", 1)]
    tiles = make_cover.plan_tiles(companies, MAPPING)
    assert [t["company"] for t in tiles] == ["OpenAI", "Anthropic", "Moonshot AI", "+2"]
    assert tiles[0]["count"] == 3 and tiles[2]["logo"] == "kimi" and tiles[-1]["logo"] is None


def test_plan_tiles_max_eight():
    slugs = ["openai", "anthropic", "google", "meta", "mistral", "deepseek", "nvidia", "ibm", "xai", "apple"]
    tiles = make_cover.plan_tiles([(s, 1) for s in slugs], {})
    assert len(tiles) == 8 and tiles[-1]["company"] == "+3"
    assert len(make_cover.plan_tiles([(s, 1) for s in slugs[:8]], {})) == 8  # ровно 8 — без «+N»


def test_company_counts_skips_rumors():
    ed = {"items": [{"kind": "news", "company": "OpenAI"}, {"kind": "news", "company": "OpenAI"},
                    {"kind": "rumor", "company": "DeepSeek"}, {"kind": "news", "company": "Meta"}]}
    assert make_cover.company_counts(ed) == [("OpenAI", 2), ("Meta", 1)]


def test_every_watchlist_logo_file_exists():
    wl = common.load_yaml("watchlist.yaml")
    slugs = [c["logo"] for c in wl["companies"] if c.get("logo")] + list(wl.get("logo_aliases", {}).values())
    missing = [s for s in slugs if not (make_cover.BRAND / "logos" / f"{s}.svg").exists()]
    assert missing == []


def test_build_cover_renders_jpeg(tmp_path):
    from PIL import Image

    tiles = make_cover.plan_tiles([("OpenAI", 3), ("Anthropic", 2), ("Canva", 1)], MAPPING)
    out = make_cover.build_cover("AI Weekly", "#7 · 1–8 ноября 2026", tiles, tmp_path / "c.jpg")
    im = Image.open(out)
    assert im.format == "JPEG" and im.size == (1280, 960)


# ---------- вариант нарезки с обложкой ----------

def posts(header="<b>AI Weekly #1 · 18–25 октября 2026</b>\n\n• a\n• b\n• c"):
    return {"issue_id": RUN, "header": header,
            "sections": [{"key": "A", "title": "🏢 Лидеры", "items": [
                {"id": f"i{n}", "html": f"<b>Новость {n}</b>\n" + "т" * 900 + f'\n<a href="https://e.com/{n}">Источник</a>'}
                for n in range(6)]}],
            "footer": "Итог.\n\n#AIWeekly"}


def test_cover_caption_is_title_only_with_tag():
    v, why = split_messages.build_cover_variant(posts())
    assert why is None
    assert v["caption"] == "<b>AI Weekly #1 · 18–25 октября 2026</b>\n\n#AIWeekly"  # без трёх событий
    assert all("AI Weekly #1" not in m["text"] and "• a" not in m["text"] for m in v["messages"])
    assert all(m["disable_preview"] for m in v["messages"])


def test_every_post_ends_with_tag_once_and_fits():
    v, _ = split_messages.build_cover_variant(posts())
    for m in v["messages"]:
        assert m["text"].endswith("#AIWeekly") and m["text"].count("#AIWeekly") == 1
        assert m["length"] <= split_messages.DEFAULT_LIMIT
    msgs, errors = split_messages.build_messages(posts())
    assert errors == [] and all(m["text"].count("#AIWeekly") == 1 for m in msgs)


def test_cover_variant_refused_for_long_title():
    v, why = split_messages.build_cover_variant(posts(header="<b>Х</b>" + "х" * 1100))
    assert v is None and "1024" in why


# ---------- публикация ----------

@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(tg_publish, "PUBLISHED", tmp_path / "state" / "published.json")
    monkeypatch.setenv("TELEGRAM_TEST_CHAT_ID", "-1001")
    rd = tmp_path / "runs" / RUN
    common.write_json(rd / "init.json", {"mode": "dry-run", "issue_number": 1, "cover": True})
    plain = [{"index": i, "text": ("HEADER\n\n" if i == 0 else "") + f"msg {i}", "disable_preview": i > 0}
             for i in range(2)]
    covered = [{"index": i, "text": f"msg {i}", "disable_preview": True} for i in range(2)]
    common.write_json(rd / "messages.json", {"messages": plain, "cover": {"caption": "HEADER", "messages": covered}})
    (rd / "cover.jpg").write_bytes(b"\xff\xd8\xff fake")
    return tmp_path


class Fake:
    def __init__(self, fail_photo=None, fail_text_at=None):
        self.calls, self.fail_photo, self.fail_text_at, self.n = [], fail_photo, fail_text_at, 0

    def __call__(self, method, payload, *, token):
        self.calls.append((method, payload))
        if method == "sendPhoto" and self.fail_photo:
            return {"ok": False, "error_code": self.fail_photo, "description": "Bad Request: wrong file"}
        if method == "sendMessage":
            if self.fail_text_at is not None and self.n == self.fail_text_at:
                self.fail_text_at = None
                return {"ok": False, "error_code": 400, "description": "Bad Request"}
            self.n += 1  # считаем только текстовые сообщения
        return {"ok": True, "result": {"message_id": 100 + len(self.calls)}}


def publish(fake):
    return tg_publish.publish(RUN, token=TOKEN, call=fake, sleep=lambda s: None,
                              cover_maker=lambda r: common.run_dir(r) / "cover.jpg")


def test_cover_sent_first_then_text_without_header(run):
    fake = Fake()
    res = publish(fake)
    assert res["status"] == "published" and res["cover"] is True
    assert [m for m, _ in fake.calls] == ["sendPhoto", "sendMessage", "sendMessage"]
    assert fake.calls[0][1]["caption"] == "HEADER" and fake.calls[0][1]["parse_mode"] == "HTML"
    assert [p["text"] for _, p in fake.calls[1:]] == ["msg 0", "msg 1"]


def test_cover_failure_falls_back_to_plain(run):
    fake = Fake(fail_photo=400)
    res = publish(fake)
    assert res["status"] == "published" and res["cover"] is False and "Bad Request" in res["cover_error"]
    texts = [p["text"] for m, p in fake.calls if m == "sendMessage"]
    assert texts[0].startswith("HEADER")  # шапка вернулась в первый пост


def test_cover_maker_crash_falls_back(run):
    fake = Fake()
    res = tg_publish.publish(RUN, token=TOKEN, call=fake, sleep=lambda s: None,
                             cover_maker=lambda r: (_ for _ in ()).throw(OSError("no font")))
    assert res["status"] == "published" and "no font" in res["cover_error"]
    assert [m for m, _ in fake.calls] == ["sendMessage", "sendMessage"]


def test_resume_after_text_failure_does_not_resend_cover(run):
    first = Fake(fail_text_at=1)
    assert publish(first)["status"] == "failed"
    second = Fake()
    res = publish(second)
    assert res["status"] == "published"
    assert [m for m, _ in second.calls] == ["sendMessage"]
    assert second.calls[0][1]["text"] == "msg 1"


def test_old_partial_issue_never_gets_cover_after_text(run):
    db = {"issues": {f"{RUN}:dry-run": {"issue_id": RUN, "mode": "dry-run", "status": "failed",
                                        "messages": [{"index": 0, "message_id": 5,
                                                      "hash": tg_publish.text_hash("HEADER\n\nmsg 0")}]}}}
    common.write_json(tg_publish.PUBLISHED, db)
    fake = Fake()
    assert publish(fake)["status"] == "published"
    assert [m for m, _ in fake.calls] == ["sendMessage"]


def test_multipart_body_contains_photo_and_caption(tmp_path):
    p = tmp_path / "c.jpg"
    p.write_bytes(b"\xff\xd8\xffJPEG")
    body, ctype = tg_publish._multipart({"chat_id": "-1", "caption": "<b>Привет</b>", "_file": ("photo", str(p))})
    assert ctype.startswith("multipart/form-data; boundary=")
    assert b'name="photo"; filename="c.jpg"' in body and b"\xff\xd8\xffJPEG" in body
    assert "<b>Привет</b>".encode() in body and json.dumps("x")  # noqa


# ---------- хэштеги по сообщениям ----------

def test_company_hashtag():
    h = split_messages.company_hashtag
    assert h("OpenAI", "#OpenAI") == "#OpenAI"
    assert h("Moonshot AI") == "#MoonshotAI" and h("Hugging Face") == "#HuggingFace"
    assert h("Исследователи (аффилиация не указана)") is None and h(None) is None


def test_each_message_gets_its_own_company_tags():
    p = {"header": "", "footer": "#OpenAI #Meta #AIWeekly",
         "sections": [{"key": "A", "title": "A", "items": [
             {"id": f"i{n}", "html": f"<b>Новость {n}</b>\n" + "т" * 1500} for n in range(6)]}]}
    tags = {"i0": "#OpenAI", "i1": "#OpenAI", "i2": "#Anthropic", "i3": "#Meta", "i4": None, "i5": "#Meta"}
    msgs, errors = split_messages.build_messages(p, item_tags=tags)
    assert errors == [] and len(msgs) >= 2
    for m in msgs:
        last = m["text"].rsplit("\n\n", 1)[1]
        assert last.endswith("#AIWeekly") and m["text"].count("#AIWeekly") == 1
        expected = [tags[f"i{n}"] for n in range(6) if f"Новость {n}<" in m["text"] and tags[f"i{n}"]]
        assert last == " ".join(dict.fromkeys(expected + ["#AIWeekly"]))
        assert m["length"] <= split_messages.TG_LIMIT
    assert "#OpenAI #Meta #AIWeekly" not in msgs[-1]["text"].rsplit("\n\n", 1)[0]  # подвал редактора убран


def test_footer_text_kept_hashtags_dropped():
    assert split_messages.drop_hashtag_lines("Спасибо!\n\n#OpenAI #AIWeekly") == "Спасибо!"


def test_ensure_deps_reinstalls_broken_pillow(monkeypatch):
    import subprocess
    import sys

    calls = []
    monkeypatch.setitem(sys.modules, "PIL", None)  # как сломанный системный Pillow: импорт падает

    def fake_run(cmd, **kw):
        calls.append(cmd)
        sys.modules.pop("PIL", None)  # «установили» рабочую копию
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert make_cover.ensure_deps()
    assert calls and "--ignore-installed" in calls[0] and "pillow" in calls[0]
