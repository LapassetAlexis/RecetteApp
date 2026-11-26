"""Colonne `share_token` de planning_history : migration idempotente depuis
data_json, synchronisation à l'écriture, recherche directe, et protection de
la page « Données » par l'auth."""

import asyncio
import json
import os

import aiosqlite
import pytest
from starlette.testclient import TestClient

import app.main as main
from app.database import Database
from tests.aides import _case, _construire, _recipe
from tests.test_auth import _reload_app

# Schéma d'avant la migration (sans share_token).
_ANCIEN_SCHEMA = """
CREATE TABLE planning_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    saison TEXT, nb_personnes INTEGER DEFAULT 4, ingredients_force TEXT,
    data_json TEXT NOT NULL, valide INTEGER NOT NULL DEFAULT 0
);
"""


def _db(tmp_path) -> Database:
    db = Database()
    db.path = str(tmp_path / "st.db")
    return db


async def _ancienne_base(path: str, data_jsons: list[str]) -> None:
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(_ANCIEN_SCHEMA)
        for dj in data_jsons:
            await conn.execute(
                "INSERT INTO planning_history (week_start, data_json, valide) VALUES ('2026-01-05', ?, 1)", (dj,)
            )
        await conn.commit()


async def _colonne(path: str) -> list[tuple]:
    async with aiosqlite.connect(path) as conn:
        return list(await conn.execute_fetchall("SELECT id, share_token FROM planning_history ORDER BY id"))


def test_migration_remplit_la_colonne_depuis_data_json(tmp_path):
    db = _db(tmp_path)
    asyncio.run(_ancienne_base(db.path, [
        json.dumps({"plats": [], "share_token": "abc"}),
        json.dumps({"plats": []}),
        "{json cassé share_token",
        json.dumps(["share_token"]),
    ]))
    asyncio.run(db.init())
    assert asyncio.run(_colonne(db.path)) == [(1, "abc"), (2, None), (3, None), (4, None)]
    # Idempotente : un second démarrage ne change rien et ne plante pas.
    asyncio.run(db.init())
    assert asyncio.run(_colonne(db.path))[0] == (1, "abc")

    async def _index():
        async with aiosqlite.connect(db.path) as conn:
            return [r[1] for r in await conn.execute_fetchall("PRAGMA index_list(planning_history)")]
    assert "idx_planning_history_share_token" in asyncio.run(_index())

    p = asyncio.run(db.get_planning_by_share_token("abc"))
    assert p["id"] == 1 and json.loads(p["data_json"])["share_token"] == "abc"
    assert asyncio.run(db.get_planning_by_share_token("inconnu")) is None
    assert asyncio.run(db.get_planning_by_share_token("")) is None


def test_colonne_synchronisee_a_chaque_ecriture(tmp_path):
    db = _db(tmp_path)
    asyncio.run(db.init())
    pid = asyncio.run(db.save_planning("2026-01-05", "", 4, "", json.dumps({"share_token": "t1"}), []))
    assert asyncio.run(db.get_planning_by_share_token("t1"))["id"] == pid

    # Le partage (routers/courses.py) écrit le jeton via update_planning_data.
    asyncio.run(db.update_planning_data(pid, json.dumps({"share_token": "t2"})))
    assert asyncio.run(db.get_planning_by_share_token("t1")) is None
    assert asyncio.run(db.get_planning_by_share_token("t2"))["id"] == pid

    asyncio.run(db.update_planning(pid, json.dumps({"plats": []}), []))
    assert asyncio.run(db.get_planning_by_share_token("t2")) is None

    asyncio.run(db.update_planning(pid, json.dumps({"share_token": "t3"}), [], week_start="2026-02-02"))
    assert asyncio.run(db.get_planning_by_share_token("t3"))["week_start"] == "2026-02-02"


def test_partage_public_via_la_colonne(client, monkeypatch):
    """Parcours complet : le lien généré par /partager ouvre la liste publique."""
    soupe = _recipe("Soupe", id="s1")

    async def _all(*a, **k):
        return [soupe]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    pid = _construire(client, [_case(1, "soir", soupe)])
    url = client.post(f"/planning/{pid}/partager").json()["url"]
    assert client.get(url).status_code == 200
    assert asyncio.run(_colonne(main.db.path)) == [(pid, url.rsplit("/", 1)[1])]


# ── Page « Données » protégée par l'auth ──────────────────────────

@pytest.fixture
def _env_auth(tmp_path):
    os.environ.update(DATABASE_PATH=str(tmp_path / "auth.db"), AUTH_USER="chef",
                      AUTH_PASSWORD="secret", NOTION_TOKEN="")
    yield
    for k in ("DATABASE_PATH", "AUTH_USER", "AUTH_PASSWORD", "NOTION_TOKEN"):
        os.environ.pop(k, None)
    _reload_app()  # app.main propre (sans auth) pour les autres tests


def test_admin_protege_par_auth(_env_auth):
    with TestClient(_reload_app().app) as c:
        for url in ("/admin", "/admin/export"):
            r = c.get(url, follow_redirects=False)
            assert r.status_code == 303 and "/login" in r.headers["location"]
        for url in ("/admin/import", "/admin/rehydrater"):
            assert c.post(url, follow_redirects=False).status_code == 303
        c.post("/login", data={"username": "chef", "password": "secret", "next": "/admin"})
        assert c.get("/admin").status_code == 200
        assert c.get("/admin/export").json()["version"] == 1
