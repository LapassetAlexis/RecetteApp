"""PWA : service worker, manifest, icônes et page de secours hors-ligne."""

import json
import re
import struct
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import app.main as main

STATIC = Path(main.__file__).parent / "static"
ICONS = {"/static/icon-180.png": 180, "/static/icon-192.png": 192, "/static/icon-512.png": 512}


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbpath = str(tmp_path / "t.db")
    main.db.path = dbpath
    monkeypatch.setattr(main.settings, "database_path", dbpath)

    async def _noop(*a, **k):
        return True
    monkeypatch.setattr(main.notion, "ensure_ingredients_field", _noop)
    with TestClient(main.app) as c:
        yield c


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_offline_page_served(client):
    r = client.get("/static/offline.html")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "hors ligne" in r.text.lower()


@pytest.mark.parametrize("path,size", ICONS.items())
def test_icons_are_png_with_expected_size(client, path, size):
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert _png_size(r.content) == (size, size)


def test_manifest_lists_png_icons(client):
    r = client.get("/static/manifest.json")
    assert r.status_code == 200
    icons = json.loads(r.text)["icons"]
    assert len(icons) == 3
    assert all(i["type"] == "image/png" for i in icons)
    assert sorted(i["purpose"] for i in icons) == ["any", "any", "maskable"]
    for icon in icons:
        res = client.get(icon["src"])
        assert res.status_code == 200
        w, h = (int(x) for x in icon["sizes"].split("x"))
        assert _png_size(res.content) == (w, h)


def test_service_worker_route(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.headers.get("service-worker-allowed") == "/"


def _sw_source() -> str:
    return (STATIC / "sw.js").read_text(encoding="utf-8")


def _precache() -> list[str]:
    src = _sw_source()
    m = re.search(r"const PRECACHE = \[(.*?)\];", src, re.S)
    assert m, "PRECACHE introuvable dans sw.js"
    offline = re.search(r"const OFFLINE_URL = '([^']+)';", src).group(1)
    return [offline if e.strip() == "OFFLINE_URL" else e.strip().strip("'")
            for e in m.group(1).split(",") if e.strip()]


def test_precache_contains_offline_page_and_not_root():
    entries = _precache()
    assert "/static/offline.html" in entries
    assert "/" not in entries


def test_precache_entries_exist(client):
    for path in _precache():
        assert client.get(path).status_code == 200, path


def test_offline_fallback_is_chained():
    src = _sw_source()
    assert "caches.match('/') ||" not in src
    assert "hit || caches.match(OFFLINE_URL)" in src
    assert "const CACHE = 'menu-planner-v5'" in src  # incrémenté par L15 puis M04
    # Ni erreurs ni redirections en cache.
    assert "!res.redirected" in src
    assert src.count("if (cacheable(res))") == 4  # + état des coches et cache à la demande (L15)
