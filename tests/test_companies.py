from companies import Directory, split_companies

WL = {
    "companies": [
        {"name": "OpenAI", "hashtag": "#OpenAI", "logo": "openai"},
        {"name": "Google", "hashtag": "#Google", "logo": "google"},
        {"name": "NVIDIA", "hashtag": "#NVIDIA", "logo": "nvidia"},
        {"name": "Meta", "hashtag": "#Meta", "logo": "meta"},
        {"name": "Облака и дата-центры", "hashtag": "#инфраструктура"},
    ],
    "logo_aliases": {"Google Cloud": "googlecloud", "Moonshot AI": "kimi", "Kimi": "kimi", "IBM": "ibm"},
}


def test_split_companies():
    assert split_companies("NVIDIA / Google Cloud") == ["NVIDIA", "Google Cloud"]
    assert split_companies("NVIDIA и Kumo") == ["NVIDIA", "Kumo"]
    assert split_companies("OpenAI") == ["OpenAI"] and split_companies(None) == []


def test_item_tags_for_compound_company():
    d = Directory(WL)
    item = {"company": "NVIDIA / Google Cloud", "hashtag": None}
    html = "<b>Google Cloud и NVIDIA: инстансы A5X</b>\nПо данным NVIDIA, Google Cloud …"
    assert d.item_tags(item, html) == ["#NVIDIA", "#GoogleCloud"]  # без лишнего #Google внутри «Google Cloud»


def test_mentions_in_text_added_after_primary():
    d = Directory(WL)
    item = {"company": "Anthropic", "hashtag": "#Anthropic"}
    html = '<b>Стандарт</b>\nИнтегрировали ChatGPT от OpenAI и сервисы Meta. <a href="https://nvidia.com">x</a>'
    assert d.item_tags(item, html) == ["#Anthropic", "#OpenAI", "#Meta"]  # ссылки не считаются


def test_aliases_share_tag_and_case_sensitive():
    d = Directory(WL)
    assert d.hashtag("Moonshot AI") == "#MoonshotAI" and d.hashtag("Kimi") == "#MoonshotAI"
    assert d.mentions("metadata и meta-данные") == []
    assert d.hashtag("Облака и дата-центры") == "#инфраструктура"
