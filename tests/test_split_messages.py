import split_messages
from split_messages import TG_LIMIT, build_messages, split_issue, validate_html, visible_len


def test_separate_title():
    assert split_messages.separate_title("<b>Заголовок</b>\nТекст") == "<b>Заголовок</b>\n\nТекст"
    assert split_messages.separate_title("<b>З</b>\n\nТекст") == "<b>З</b>\n\nТекст"  # не удваивает


def item(i, n=300):
    return {"id": f"id{i}", "html": f"<b>Новость {i}</b>\n" + "т" * n + f'\n<a href="https://e.com/{i}?a=1&amp;b=2">Источник</a>'}


def issue(sections):
    return {"issue_id": "2026-10-25_weekly", "header": "<b>AI Weekly #1 · 19–25 октября</b>\n\n• a\n• b\n• c",
            "sections": sections, "footer": "Итог недели.\n\n#OpenAI #AIWeekly"}


def test_valid_html_passes():
    assert validate_html('<b>x</b> <i>y</i> <a href="https://a.b/c?x=1&amp;y=2">s</a> &lt;3') == []


def test_invalid_html_detected():
    assert validate_html("<div>x</div>")
    assert validate_html("<b>x")
    assert validate_html("a < b")
    assert validate_html("Q&A")
    assert validate_html('<a href="javascript:alert(1)">x</a>')
    assert validate_html("<b><i>x</b></i>")
    assert validate_html("x<br/>y")


def test_visible_len_ignores_tags_and_counts_utf16():
    assert visible_len("<b>ab</b>") == 2
    assert visible_len("&amp;") == 1
    assert visible_len("🏢") == 2  # суррогатная пара


def test_small_issue_is_one_message():
    msgs, errors = build_messages(issue([{"key": "A", "title": "🏢 Лидеры", "items": [item(1), item(2)]}]))
    assert errors == []
    assert len(msgs) == 1
    assert msgs[0]["disable_preview"] is False


def test_split_respects_limit_and_never_breaks_items():
    sections = [{"key": k, "title": f"Раздел {k}", "items": [item(f"{k}{i}", 600) for i in range(6)]}
                for k in "ABCDEF"]
    posts = issue(sections)
    msgs, errors = build_messages(posts)
    assert errors == []
    assert len(msgs) > 1
    joined = "\n\n".join(m["text"] for m in msgs)
    for s in sections:
        for it in s["items"]:
            # каждая новость целиком и ровно один раз: заголовок, текст и ссылка в одном сообщении
            title = it["html"].split("\n")[0][3:-4]
            href = it["html"].rsplit('href="', 1)[1].split('"')[0]
            holder = [m["text"] for m in msgs if f". {title}</b>" in m["text"]]
            assert len(holder) == 1 and holder[0].count(f'href="{href}"') == 1
            assert joined.count(f". {title}</b>") == 1
    for m in msgs:
        assert m["length"] <= 4000 < TG_LIMIT
    assert [m["disable_preview"] for m in msgs] == [False] + [True] * (len(msgs) - 1)
    assert msgs[0]["text"].startswith("<b>AI Weekly")
    assert msgs[-1]["text"].endswith("#AIWeekly")


def test_long_section_repeats_title():
    posts = issue([{"key": "A", "title": "Лидеры", "items": [item(i, 900) for i in range(8)]}])
    texts = split_issue(posts)
    assert any("Лидеры (продолжение)" in t for t in texts[1:])


def test_section_moves_whole_when_possible():
    posts = issue([{"key": "A", "title": "A", "items": [item(i, 1100) for i in range(3)]},
                   {"key": "B", "title": "B", "items": [item(f"b{i}", 1100) for i in range(3)]}])
    texts = split_issue(posts)
    b_msgs = [t for t in texts if "<b>B</b>" in t]
    assert len(b_msgs) == 1 and all(f"Новость b{i}" in b_msgs[0] for i in range(3))


def test_oversized_item_is_error():
    posts = issue([{"key": "A", "title": "A", "items": [item(1, 5000)]}])
    msgs, errors = build_messages(posts)
    assert errors and not msgs


ITEM = ('<b>Anthropic передала стандарт</b>\nТекст новости.\n'
        '<i>Почему важно:</i> за стандартом теперь фонд.\n<a href="https://a.com/x">Источник</a>')


def test_decorate_item_variant_b_with_number_and_date():
    out = split_messages.decorate_item(ITEM, 1, "22 октября")
    assert out == ('<b>1. Anthropic передала стандарт</b>\n<i>Дата публикации: 22 октября</i>\n\nТекст новости.\n\n'
                   '<blockquote><b>Почему важно:</b> за стандартом теперь фонд.</blockquote>\n'
                   '<a href="https://a.com/x">→ Источник</a>')
    assert validate_html(out) == []


def test_decorate_item_without_date_and_unknown_shape():
    assert split_messages.decorate_item(ITEM, 3).startswith("<b>3. Anthropic передала стандарт</b>\n\nТекст")
    assert split_messages.decorate_item("Просто текст\nещё", 1) == "Просто текст\nещё"  # не по шаблону — как есть


def test_numbering_runs_through_whole_issue():
    p = {"header": "", "footer": "", "sections": [
        {"key": "A", "title": "A", "items": [{"id": "a", "html": ITEM}, {"id": "b", "html": ITEM}]},
        {"key": "G", "title": "G", "items": [{"id": "c", "html": ITEM}]}]}
    msgs, errors = build_messages(p, item_dates={"a": "21 октября"})
    text = "\n".join(m["text"] for m in msgs)
    assert errors == [] and "<b>1. Anthropic" in text and "<b>2. Anthropic" in text and "<b>3. Anthropic" in text
    assert text.count("<i>Дата публикации: 21 октября</i>") == 1


def test_date_label():
    assert split_messages.date_label("2026-10-22") == "22 октября"
    assert split_messages.date_label("2026-11-01T14:00:00Z") == "1 ноября"
    assert split_messages.date_label(None) is None


def test_decorate_item_compact():
    out = split_messages.decorate_item(ITEM, 1, "22 октября", compact=True)
    assert out == ('<b>1. Anthropic передала стандарт</b>\n<i>Дата публикации: 22 октября</i>\nТекст новости.\n'
                   '<blockquote><b>Почему важно:</b> за стандартом теперь фонд.</blockquote>\n'
                   '<a href="https://a.com/x">→ Источник</a>')
