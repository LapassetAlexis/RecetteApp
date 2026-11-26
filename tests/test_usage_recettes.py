"""Compteur « Cuisinée N fois » (M03, N12, décision D9) : un repas groupé
compte une fois, les repas à venir ne comptent pas."""

import asyncio
from datetime import date, timedelta

import pytest

from app.database import Database

TODAY = date(2026, 11, 26)  # jeudi


@pytest.fixture
def db(tmp_path):
    d = Database()
    d.path = str(tmp_path / "usage.db")
    asyncio.run(d.init())
    return d


def _r(nid, jour, moment="midi", nom="Recette"):
    return {"notion_id": nid, "recipe_name": nom, "jour": jour, "moment": moment}


def _valid(db, week_start, recipes):
    pid = asyncio.run(db.save_planning(week_start, "", 2, "", "{}", recipes))
    asyncio.run(db.mark_planning_valid(pid))
    return pid


def _usage(db, today=TODAY):
    return asyncio.run(db.get_recipe_usage(today=today))


def test_poulet_groupe_sur_deux_jours_passes_compte_une_fois(db):
    # Semaine du 23/11 : poulet groupé lundi + mardi midi.
    _valid(db, "2026-11-23", [_r("poulet", 1), _r("poulet", 2)])
    assert _usage(db) == {"poulet": {"count": 1, "last": "2026-11-24"}}


def test_semaine_validee_a_venir_non_comptee(db):
    _valid(db, "2026-11-30", [_r("poulet", 1), _r("soupe", 3, "soir")])
    assert _usage(db) == {}


def test_repas_du_jour_compte(db):
    _valid(db, "2026-11-23", [_r("poulet", 4)])  # jeudi 26/11 = aujourd'hui
    assert _usage(db) == {"poulet": {"count": 1, "last": "2026-11-26"}}


def test_groupe_a_cheval_sur_aujourdhui(db):
    # Groupé jeudi + vendredi : cuisiné jeudi (aujourd'hui), vendredi pas encore.
    _valid(db, "2026-11-23", [_r("poulet", 4), _r("poulet", 5)])
    assert _usage(db) == {"poulet": {"count": 1, "last": "2026-11-26"}}


def test_midis_non_consecutifs_comptent_deux_fois(db):
    _valid(db, "2026-11-16", [_r("poulet", 1), _r("poulet", 3)])
    assert _usage(db)["poulet"]["count"] == 2


def test_deux_groupes_dans_la_semaine(db):
    _valid(db, "2026-11-16", [_r("poulet", 1), _r("poulet", 2), _r("poulet", 4), _r("poulet", 5)])
    assert _usage(db)["poulet"] == {"count": 2, "last": "2026-11-20"}


def test_soirs_comptent_chacun(db):
    # Seuls les midis se groupent ; deux soirs consécutifs = deux repas.
    _valid(db, "2026-11-16", [_r("soupe", 1, "soir"), _r("soupe", 2, "soir")])
    assert _usage(db)["soupe"]["count"] == 2


def test_midi_et_soir_meme_jour(db):
    _valid(db, "2026-11-16", [_r("pates", 1), _r("pates", 1, "soir")])
    assert _usage(db)["pates"]["count"] == 2


def test_meme_recette_dans_deux_plannings(db):
    # Dimanche d'une semaine + lundi de la suivante : plannings différents.
    _valid(db, "2026-11-09", [_r("poulet", 7)])
    _valid(db, "2026-11-16", [_r("poulet", 1)])
    assert _usage(db)["poulet"] == {"count": 2, "last": "2026-11-16"}


def test_brouillon_et_repas_libre_ignores(db):
    asyncio.run(db.save_planning("2026-11-16", "", 2, "", "{}", [_r("poulet", 1)]))
    _valid(db, "2026-11-16", [_r("", 1, nom="Pizzas")])
    assert _usage(db) == {}


def test_week_start_invalide_ignore(db):
    _valid(db, "pas-une-date", [_r("poulet", 1)])
    _valid(db, "2026-11-16", [_r("soupe", 2)])
    assert _usage(db) == {"soupe": {"count": 1, "last": "2026-11-17"}}


def test_today_par_defaut_est_aujourdhui(db):
    hier = date.today() - timedelta(days=1)
    demain_semaine = (date.today() + timedelta(days=7)).isoformat()
    _valid(db, hier.isoformat(), [_r("passe", 1)])
    _valid(db, demain_semaine, [_r("futur", 1)])
    usage = asyncio.run(db.get_recipe_usage())
    assert usage == {"passe": {"count": 1, "last": hier.isoformat()}}
