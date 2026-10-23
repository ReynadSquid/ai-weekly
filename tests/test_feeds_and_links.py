from datetime import datetime, timezone

import check_links
import fetch_feeds
import net
from common import normalize_url, url_id

SRC = {"name": "test", "source_type": "official", "company": "OpenAI"}

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Introducing GPT-6</title><link>https://openai.com/index/gpt-6/</link>
<pubDate>Wed, 22 Oct 2026 17:00:00 GMT</pubDate><description>&lt;p&gt;New &lt;b&gt;model&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>No link</title></item></channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
<entry><title>Gemini update</title><link rel="alternate" href="https://blog.google/x"/>
<updated>2026-10-21T08:00:00Z</updated><summary>Short</summary></entry></feed>"""

ARXIV = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>http://arxiv.org/abs/2610.01234v2</id><title>A Paper</title><published>2026-10-20T00:00:00Z</published>
<summary>We propose</summary><author><name>A. Author</name></author></entry></feed>"""

TG = """<div class="tgme_widget_message_wrap"><div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="ai_newz/100">
<div class="tgme_widget_message_text js-message_text" dir="auto">Anthropic выпустила новую модель<br/>Подробности: <a href="https://www.anthropic.com/news/x" target="_blank">тут</a> и <a href="https://t.me/other/1">там</a></div>
<a class="tgme_widget_message_date" href="https://t.me/ai_newz/100"><time datetime="2026-10-22T10:00:00+00:00" class="time">10:00</time></a></div></div>
<div class="tgme_widget_message js-widget_message" data-post="ai_newz/101">
<div class="tgme_widget_message_text js-message_text">Слух без ссылки</div><time datetime="2026-10-23T10:00:00+00:00"></time></div>"""


def test_parse_rss():
    items = fetch_feeds.parse_rss(RSS, SRC)
    assert len(items) == 1
    it = items[0]
    assert it["url"] == "https://openai.com/index/gpt-6/"
    assert it["published_at"] == "2026-10-22T17:00:00Z"
    assert it["raw_excerpt"] == "New model"
    assert it["company"] == "OpenAI" and it["id"] == url_id(it["url"])


def test_parse_atom():
    items = fetch_feeds.parse_rss(ATOM, SRC)
    assert items[0]["url"] == "https://blog.google/x" and items[0]["published_at"] == "2026-10-21T08:00:00Z"


def test_parse_arxiv_strips_version():
    it = fetch_feeds.parse_arxiv(ARXIV, {"name": "arxiv.org"})[0]
    assert it["url"] == "https://arxiv.org/abs/2610.01234" and it["authors"] == ["A. Author"]


def test_parse_tg_uses_primary_source():
    posts = fetch_feeds.parse_tg(TG, "ai_newz")
    assert len(posts) == 2
    assert posts[0]["url"] == "https://www.anthropic.com/news/x"
    assert posts[0]["discovered_via"] == "https://t.me/ai_newz/100"
    assert posts[0]["source_type"] == "telegram"
    assert posts[1]["needs_primary_source"] is True


def test_parse_hn_filters_points():
    data = {"hits": [{"objectID": "1", "title": "Big", "url": "https://x.com/a", "points": 300,
                      "created_at": "2026-10-22T00:00:00Z"},
                     {"objectID": "2", "title": "Small", "points": 10}]}
    items = fetch_feeds.parse_hn(data, {"name": "hn"}, 150)
    assert [i["title"] for i in items] == ["Big"]


def test_in_window():
    since = datetime(2026, 10, 18, tzinfo=timezone.utc)
    until = datetime(2026, 10, 25, tzinfo=timezone.utc)
    assert fetch_feeds.in_window({"published_at": "2026-10-20T00:00:00Z"}, since, until)
    assert not fetch_feeds.in_window({"published_at": "2026-10-01T00:00:00Z"}, since, until)
    assert fetch_feeds.in_window({"published_at": None}, since, until)


def test_normalize_url():
    assert normalize_url("http://www.Example.com/a/?utm_source=x&b=2#frag") == "https://example.com/a?b=2"


def test_classify_links():
    prot = ["openai.com"]
    assert check_links.classify("https://a.com/post/1", 200, "https://a.com/post/1", prot) == "ok"
    assert check_links.classify("https://a.com/post/1", 200, "https://a.com/", prot) == "homepage"
    assert check_links.classify("https://openai.com/index/x", 403, None, prot) == "unverifiable"
    assert check_links.classify("https://a.com/x", 403, None, prot) == "broken"
    assert check_links.classify("https://a.com/x", None, None, prot) == "broken"


def test_get_with_retries_three_attempts(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return net.Response(url, 503, b"", {})

    monkeypatch.setattr(net, "http_get", fake_get)
    sleeps = []
    try:
        net.get_with_retries("https://x.test/", retries=3, pause=1, sleep=sleeps.append)
        assert False, "должно упасть"
    except net.FetchError:
        pass
    assert len(calls) == 3 and sleeps == [1, 2]


def test_get_with_retries_no_retry_on_404(monkeypatch):
    calls = []
    monkeypatch.setattr(net, "http_get", lambda url, **kw: calls.append(1) or net.Response(url, 404, b"", {}))
    try:
        net.get_with_retries("https://x.test/", sleep=lambda s: None)
    except net.FetchError:
        pass
    assert len(calls) == 1
