"""Réglages uniques du foyer (table app_prefs) : lecture tolérante et écriture."""

import asyncio

import aiosqlite

from app.database import Database


def _db(tmp_path) -> Database:
    db = Database()
    db.path = str(tmp_path / "prefs.db")
    asyncio.run(db.init())
    return db


def test_get_app_prefs_vide_par_defaut(tmp_path):
    assert asyncio.run(_db(tmp_path).get_app_prefs()) == {}


def test_save_puis_get_app_prefs(tmp_path):
    db = _db(tmp_path)
    prefs = {"placard": ["sel", "poivre", "huile d'olive"], "ordre_rayons": ["Frais", "Épicerie"]}
    asyncio.run(db.save_app_prefs(prefs))
    assert asyncio.run(db.get_app_prefs()) == prefs


def test_save_app_prefs_remplace_la_ligne_unique(tmp_path):
    db = _db(tmp_path)
    asyncio.run(db.save_app_prefs({"placard": ["sel"]}))
    asyncio.run(db.save_app_prefs({"ordre_rayons": ["Frais"]}))
    assert asyncio.run(db.get_app_prefs()) == {"ordre_rayons": ["Frais"]}

    async def _count():
        async with aiosqlite.connect(db.path) as conn:
            rows = await conn.execute_fetchall("SELECT COUNT(*) FROM app_prefs")
        return rows[0][0]
    assert asyncio.run(_count()) == 1


def test_get_app_prefs_tolere_un_json_illisible(tmp_path):
    db = _db(tmp_path)

    async def _ecrit(raw: str):
        async with aiosqlite.connect(db.path) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO app_prefs (id, data_json) VALUES (1, ?)", (raw,),
            )
            await conn.commit()

    asyncio.run(_ecrit("{pas du json"))
    assert asyncio.run(db.get_app_prefs()) == {}
    asyncio.run(_ecrit("[1, 2]"))  # JSON valide mais pas un objet
    assert asyncio.run(db.get_app_prefs()) == {}
