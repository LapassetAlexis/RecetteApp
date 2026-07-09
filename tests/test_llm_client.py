"""Tests de robustesse du client LLM : retry/backoff, fallback, validation.

NB : on référence tout via le module `app.llm_client` (et non des imports figés)
car d'autres tests (test_auth) rechargent le module ; sans ça l'identité de
`LLMError`/`LLMClient` divergerait entre modules rechargés.
"""

import asyncio

import httpx
import pytest

import app.llm_client as L


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://x")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(status, request=req))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Neutralise le backoff pour des tests rapides et déterministes."""
    async def _fast(*_a, **_k):
        return None
    monkeypatch.setattr(L.asyncio, "sleep", _fast)


def _client(provider="ollama"):
    c = L.LLMClient()
    c.provider = provider
    return c


def test_chat_retries_then_succeeds(monkeypatch):
    c = _client()
    calls = {"n": 0}
    async def fake(provider, *a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.TimeoutException("timeout")
        return "ok"
    monkeypatch.setattr(c, "_chat_call", fake)
    assert asyncio.run(c._chat("s", "u")) == "ok"
    assert calls["n"] == 3  # 2 échecs transitoires + 1 succès


def test_chat_fatal_no_retry(monkeypatch):
    c = _client()
    calls = {"n": 0}
    async def fake(provider, *a, **k):
        calls["n"] += 1
        raise _http_error(400)
    monkeypatch.setattr(c, "_chat_call", fake)
    with pytest.raises(L.LLMError):
        asyncio.run(c._chat("s", "u"))
    assert calls["n"] == 1  # fail-fast : aucune nouvelle tentative


def test_chat_exhausts_then_llmerror(monkeypatch):
    c = _client()
    calls = {"n": 0}
    async def fake(provider, *a, **k):
        calls["n"] += 1
        raise _http_error(503)
    monkeypatch.setattr(c, "_chat_call", fake)
    with pytest.raises(L.LLMError):
        asyncio.run(c._chat("s", "u"))
    assert calls["n"] == 3  # _MAX_RETRIES


def test_chat_fallback_ollama(monkeypatch):
    c = _client("gemini")
    async def fake(provider, *a, **k):
        if provider == "gemini":
            raise _http_error(503)
        return "depuis-ollama"
    monkeypatch.setattr(c, "_chat_call", fake)
    assert asyncio.run(c._chat("s", "u")) == "depuis-ollama"


def test_complete_repairs_invalid_json(monkeypatch):
    c = _client()
    outs = iter(["pas du json", '{"ingredients": [{"nom": "riz", "quantite": "100", "unite": "g"}]}'])
    async def fake_chat(system, user, **k):
        return next(outs)
    monkeypatch.setattr(c, "_chat", fake_chat)
    r = asyncio.run(c._complete("s", "u", response_model=L.IngredientsResponse,
                                label="ing", core_field="ingredients"))
    assert isinstance(r, L.IngredientsResponse) and r.ingredients[0].nom == "riz"


def test_complete_gives_up_after_repair(monkeypatch):
    c = _client()
    async def fake_chat(system, user, **k):
        return "toujours pas du json"
    monkeypatch.setattr(c, "_chat", fake_chat)
    with pytest.raises(L.LLMError):
        asyncio.run(c._complete("s", "u", response_model=L.RecipeExtraction,
                                label="rx", core_field="nom"))


def test_recipe_extraction_coerces_types():
    # ingredients en string -> liste ; instructions en liste -> string
    m = L.RecipeExtraction.model_validate({
        "nom": "Curry", "ingredients": "riz\npoulet", "instructions": ["Cuire.", "Servir."],
        "tags": "Épicé",
    })
    assert m.ingredients == ["riz", "poulet"]
    assert m.instructions == "Cuire.\nServir."
    assert m.tags == ["Épicé"]
