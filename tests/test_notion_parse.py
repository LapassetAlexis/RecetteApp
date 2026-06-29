"""Tests du parsing des pages Notion (_parse_page), sans réseau."""

from app.notion_client import NotionClient

notion = NotionClient()


def _page(props):
    return {"id": "pid", "url": "https://notion.so/pid", "properties": props}


def test_parse_full_page():
    page = _page({
        "Nom": {"type": "title", "title": [{"plain_text": "Tarte"}]},
        "URL": {"type": "url", "url": "https://x.fr"},
        "Repas": {"select": {"name": "Dessert"}},
        "Tag": {"multi_select": [{"name": "Fun"}, {"name": "Diet"}]},
        "Note": {"select": {"name": "⭐⭐⭐"}},
        "État": {"status": {"name": "À essayer"}},
        "Moment": {"select": {"name": "Soir"}},
    })
    r = notion._parse_page(page)
    assert r["nom"] == "Tarte"
    assert r["url"] == "https://x.fr"
    assert r["repas"] == "Dessert"
    assert r["tags"] == ["Fun", "Diet"]
    assert r["note"] == "⭐⭐⭐"
    assert r["etat"] == "À essayer"
    assert r["moment"] == "Soir"


def test_parse_missing_columns_does_not_crash():
    # Régression : colonnes absentes/renommées faisaient un KeyError.
    r = notion._parse_page(_page({"Nom": {"type": "title", "title": [{"plain_text": "X"}]}}))
    assert r["nom"] == "X"
    assert r["url"] == "" and r["repas"] == "" and r["tags"] == []
    assert r["note"] == "" and r["etat"] == "" and r["moment"] == ""


def test_parse_empty_selects():
    page = _page({
        "Nom": {"type": "title", "title": []},
        "URL": {"type": "url", "url": None},
        "Repas": {"select": None},
        "Tag": {"multi_select": []},
        "Note": {"select": None},
        "État": {"status": None},
    })
    r = notion._parse_page(page)
    assert r["nom"] == "" and r["repas"] == "" and r["tags"] == []
