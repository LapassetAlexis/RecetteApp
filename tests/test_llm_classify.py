"""Tests classification type/tags + parsing JSON LLM (sans réseau)."""

import asyncio

from app.llm_client import LLMClient

client = LLMClient()


def test_parse_json_variants():
    assert client._parse_json('{"a": 1}') == {"a": 1}
    assert client._parse_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert client._parse_json('blabla {"a": 3} fin') == {"a": 3}
    assert client._parse_json("rien du tout") == {}


def test_classify_success(monkeypatch):
    async def _fake_chat(system, user, **kw):
        return '{"type_repas": "Plat", "base": ["Viande", "Inexistant"], "tags": ["Soupe", "Bidon"]}'
    monkeypatch.setattr(client, "_chat", _fake_chat)
    type_repas, tags, base = asyncio.run(client._classify_type_tags("Boeuf bourguignon", ["boeuf", "vin"], []))
    assert type_repas == "Plat"
    assert tags == ["Soupe"]   # "Bidon" filtré (hors TAG_OPTIONS)
    assert base == ["Viande"]  # "Inexistant" filtré (hors BASE_OPTIONS)


def test_classify_invalid_type_dropped(monkeypatch):
    async def _fake_chat(system, user, **kw):
        return '{"type_repas": "PasUnType", "base": [], "tags": []}'
    monkeypatch.setattr(client, "_chat", _fake_chat)
    type_repas, tags, base = asyncio.run(client._classify_type_tags("X", [], []))
    assert type_repas == "Plat" and tags == [] and base == []  # type invalide -> repli heuristique


def test_classify_fallback_on_error(monkeypatch):
    async def _boom(system, user, **kw):
        raise RuntimeError("LLM down")
    monkeypatch.setattr(client, "_chat", _boom)
    # repli sans LLM : type deviné (défaut Plat) + base par mots-clés
    type_repas, tags, base = asyncio.run(client._classify_type_tags("Steak de boeuf", ["boeuf"], []))
    assert type_repas == "Plat" and "Viande" in base
