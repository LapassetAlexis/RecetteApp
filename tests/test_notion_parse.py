"""Tests du parsing des pages Notion (_parse_page) + cache, sans réseau."""

import asyncio

from app.notion_client import NotionClient

notion = NotionClient()


def test_get_all_recipes_uses_cache(monkeypatch):
    n = NotionClient()
    calls = {"c": 0}

    async def _fetch():
        calls["c"] += 1
        return [{"nom": "X"}]

    monkeypatch.setattr(n, "_fetch_all_recipes", _fetch)
    asyncio.run(n.get_all_recipes())
    asyncio.run(n.get_all_recipes())   # servi par le cache
    assert calls["c"] == 1
    n.invalidate_cache()
    asyncio.run(n.get_all_recipes())   # refetch après invalidation
    assert calls["c"] == 2
    asyncio.run(n.get_all_recipes(force=True))
    assert calls["c"] == 3


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
    # Repli sur select mono : liste à 1 élément.
    assert r["repas"] == ["Dessert"]
    assert r["tags"] == ["Fun", "Diet"]
    assert r["note"] == "⭐⭐⭐"
    assert r["etat"] == "À essayer"
    assert r["moment"] == "Soir"


def test_parse_missing_columns_does_not_crash():
    # Régression : colonnes absentes/renommées faisaient un KeyError.
    r = notion._parse_page(_page({"Nom": {"type": "title", "title": [{"plain_text": "X"}]}}))
    assert r["nom"] == "X"
    assert r["url"] == "" and r["repas"] == [] and r["tags"] == []
    assert r["note"] == "" and r["etat"] == "" and r["moment"] == ""


def test_parse_cover_image():
    page = _page({"Nom": {"type": "title", "title": [{"plain_text": "X"}]}})
    page["cover"] = {"type": "external", "external": {"url": "http://img.jpg"}}
    assert notion._parse_page(page)["image"] == "http://img.jpg"
    page["cover"] = {"type": "file", "file": {"url": "http://up.jpg"}}
    assert notion._parse_page(page)["image"] == "http://up.jpg"
    del page["cover"]
    assert notion._parse_page(page)["image"] == ""


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
    assert r["nom"] == "" and r["repas"] == [] and r["tags"] == []


def test_parse_repas_multi_select():
    # Nouveau format : Repas en multi_select (plusieurs types).
    page = _page({
        "Nom": {"type": "title", "title": [{"plain_text": "Cookie"}]},
        "Repas": {"multi_select": [{"name": "Goûter"}, {"name": "Dessert"}]},
    })
    r = notion._parse_page(page)
    assert r["repas"] == ["Goûter", "Dessert"]
