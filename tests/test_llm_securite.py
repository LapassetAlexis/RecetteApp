"""Durcissement du client LLM (L04) : clé Gemini hors des URL, fetch de pages
protégé contre le SSRF par redirection. Réseau simulé par `httpx.MockTransport`,
DNS simulé ; pas de pytest-asyncio, on passe par asyncio.run().

On référence le module (`app.llm_client as L`) plutôt que des noms importés :
test_auth recharge le module, les identités divergeraient sinon.
"""

import asyncio
import socket

import httpx
import pytest

import app.llm_client as L

PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def fake_dns(monkeypatch):
    """Tout nom d'hôte se résout vers une IP publique ; les IP littérales
    se résolvent vers elles-mêmes (127.0.0.1 reste donc interne)."""
    def _getaddrinfo(host, *_a, **_k):
        try:
            socket.inet_aton(host)
            ip = host
        except OSError:
            ip = PUBLIC_IP
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
    monkeypatch.setattr(L.socket, "getaddrinfo", _getaddrinfo)


@pytest.fixture
def transport(monkeypatch):
    """Branche un MockTransport sur tous les httpx.AsyncClient créés par le module.
    `routes` associe une URL complète à une réponse ; `seen` garde les requêtes."""
    state = {"routes": {}, "seen": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["seen"].append(request)
        return state["routes"].get(str(request.url), httpx.Response(404))

    real = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(L.httpx, "AsyncClient", _client)
    return state


def _redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"location": location})


# ── Clé Gemini ───────────────────────────────────────────────

def test_gemini_key_in_header_not_url(monkeypatch, transport):
    monkeypatch.setattr(L.settings, "llm_provider", "gemini")
    monkeypatch.setattr(L.settings, "gemini_api_key", "AIza-SECRET")
    client = L.LLMClient()
    assert "AIza-SECRET" not in client._url
    assert "key=" not in client._url

    transport["routes"][client._url] = httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": "bonjour"}]}}],
    })
    assert asyncio.run(client._chat_gemini("sys", "user")) == "bonjour"

    req = transport["seen"][-1]
    assert "AIza-SECRET" not in str(req.url)
    assert req.headers["x-goog-api-key"] == "AIza-SECRET"


@pytest.mark.parametrize("provider", ["gemini", "groq", "ollama"])
def test_no_provider_url_contains_key(monkeypatch, provider):
    monkeypatch.setattr(L.settings, "llm_provider", provider)
    monkeypatch.setattr(L.settings, "gemini_api_key", "AIza-SECRET")
    monkeypatch.setattr(L.settings, "groq_api_key", "gsk-SECRET")
    url = L.LLMClient()._url
    assert "SECRET" not in url


# ── _safe_fetch_html : redirections ─────────────────────────

def test_fetch_plain_public_page(fake_dns, transport):
    transport["routes"]["https://recettes.example/tarte"] = httpx.Response(200, text="<html>ok</html>")
    assert asyncio.run(L._safe_fetch_html("https://recettes.example/tarte")) == "<html>ok</html>"


def test_fetch_follows_public_redirect(fake_dns, transport):
    transport["routes"]["https://recettes.example/a"] = _redirect("/b", 301)
    transport["routes"]["https://recettes.example/b"] = httpx.Response(200, text="page b")
    assert asyncio.run(L._safe_fetch_html("https://recettes.example/a")) == "page b"


def test_fetch_redirect_to_loopback_is_blocked(fake_dns, transport):
    transport["routes"]["https://recettes.example/piege"] = _redirect("http://127.0.0.1/admin")
    transport["routes"]["http://127.0.0.1/admin"] = httpx.Response(200, text="SECRET INTERNE")
    assert asyncio.run(L._safe_fetch_html("https://recettes.example/piege")) == ""
    # La cible interne n'a jamais été contactée.
    assert all(r.url.host != "127.0.0.1" for r in transport["seen"])


def test_fetch_redirect_to_metadata_ip_is_blocked(fake_dns, transport):
    transport["routes"]["https://recettes.example/x"] = _redirect("http://169.254.169.254/latest/meta-data/")
    assert asyncio.run(L._safe_fetch_html("https://recettes.example/x")) == ""
    assert len(transport["seen"]) == 1


def test_fetch_redirect_to_non_http_scheme_is_blocked(fake_dns, transport):
    transport["routes"]["https://recettes.example/x"] = _redirect("file:///etc/passwd")
    assert asyncio.run(L._safe_fetch_html("https://recettes.example/x")) == ""


def test_fetch_stops_after_three_redirects(fake_dns, transport):
    for i in range(5):
        transport["routes"][f"https://recettes.example/{i}"] = _redirect(f"/{i + 1}")
    transport["routes"]["https://recettes.example/5"] = httpx.Response(200, text="trop loin")
    assert asyncio.run(L._safe_fetch_html("https://recettes.example/0")) == ""
    assert len(transport["seen"]) == L._MAX_REDIRECTS + 1


def test_fetch_three_redirects_is_allowed(fake_dns, transport):
    for i in range(3):
        transport["routes"][f"https://recettes.example/{i}"] = _redirect(f"/{i + 1}")
    transport["routes"]["https://recettes.example/3"] = httpx.Response(200, text="arrivé")
    assert asyncio.run(L._safe_fetch_html("https://recettes.example/0")) == "arrivé"


def test_fetch_http_error_returns_empty(fake_dns, transport):
    transport["routes"]["https://recettes.example/500"] = httpx.Response(500)
    assert asyncio.run(L._safe_fetch_html("https://recettes.example/500")) == ""


# ── extract_ingredients passe par le fetch durci ─────────────

def test_extract_ingredients_uses_safe_fetch(monkeypatch, fake_dns, transport):
    transport["routes"]["https://recettes.example/piege"] = _redirect("http://127.0.0.1/admin")
    transport["routes"]["http://127.0.0.1/admin"] = httpx.Response(200, text="SECRET INTERNE")

    prompts = []

    async def _fake_complete(system, user, **_k):
        prompts.append(user)
        return L.IngredientsResponse(ingredients=[])

    client = L.LLMClient()
    monkeypatch.setattr(client, "_complete", _fake_complete)
    asyncio.run(client.extract_ingredients("Tarte", url="https://recettes.example/piege"))

    assert all(r.url.host != "127.0.0.1" for r in transport["seen"])
    assert "SECRET INTERNE" not in prompts[0]
    assert "Aucun contenu disponible" in prompts[0]


def test_extract_ingredients_reads_public_page(monkeypatch, fake_dns, transport):
    html = """<script type="application/ld+json">
    {"@type":"Recipe","name":"Tarte","recipeIngredient":["3 pommes","200 g de farine"]}
    </script>"""
    transport["routes"]["https://recettes.example/tarte"] = httpx.Response(200, text=html)

    prompts = []

    async def _fake_complete(system, user, **_k):
        prompts.append(user)
        return L.IngredientsResponse(ingredients=[])

    client = L.LLMClient()
    monkeypatch.setattr(client, "_complete", _fake_complete)
    asyncio.run(client.extract_ingredients("Tarte", url="https://recettes.example/tarte"))
    assert "200 g de farine" in prompts[0]
