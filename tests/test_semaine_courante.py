"""M07 — « Cette semaine » (`GET /semaine`), menu (« Cette semaine »,
« Données ») et bandeau de la page « Données » sans auth (D3, D6)."""

import asyncio
import json
import re
from datetime import date, timedelta

import pytest
from starlette.testclient import TestClient

from app import main
from app.routers.pages import planning_en_cours
from tests.test_auth import _client


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


def _p(pid, week_start, nb_jours=None, plats=()):
    data = {"plats": list(plats)}
    if nb_jours is not None:
        data["nb_jours"] = nb_jours
    return {"id": pid, "week_start": week_start, "data_json": json.dumps(data)}


def _creer(week_start: str, nb_jours: int, valide: bool = True) -> int:
    async def go():
        pid = await main.db.save_planning(week_start, "", 2, "", json.dumps({"plats": [], "nb_jours": nb_jours}), [])
        if valide:
            await main.db.mark_planning_valid(pid)
        return pid
    return asyncio.run(go())


# ── Choix du planning (fonction pure) ───────────────────────────────────

def test_planning_dont_la_periode_contient_aujourdhui():
    jeudi = date(2026, 11, 26)
    liste = [_p(3, "2026-11-30", 7), _p(2, "2026-11-23", 7), _p(1, "2026-11-16", 7)]
    assert planning_en_cours(liste, jeudi)["id"] == 2


def test_periode_courte_et_bornes():
    liste = [_p(2, "2026-11-23", 3)]  # du 23 au 25
    assert planning_en_cours(liste, date(2026, 11, 23))["id"] == 2
    assert planning_en_cours(liste, date(2026, 11, 25))["id"] == 2
    # hors période : repli sur le plus récent (le seul)
    assert planning_en_cours([_p(2, "2026-11-23", 3), _p(1, "2026-11-26", 2)],
                             date(2026, 11, 27))["id"] == 1


def test_repli_sur_le_plus_recent_puis_rien():
    liste = [_p(5, "2026-10-05", 7), _p(4, "2026-09-28", 7)]
    assert planning_en_cours(liste, date(2026, 11, 26))["id"] == 5
    assert planning_en_cours([], date(2026, 11, 26)) is None


def test_plus_recent_gagne_si_deux_periodes_se_chevauchent():
    liste = [_p(9, "2026-11-25", 7), _p(8, "2026-11-23", 7)]
    assert planning_en_cours(liste, date(2026, 11, 26))["id"] == 9


def test_donnees_illisibles_ou_anciennes():
    # date illisible : ignorée ; data_json cassé : période de 7 jours par défaut
    liste = [{"id": 2, "week_start": "n/a", "data_json": "{}"},
             {"id": 1, "week_start": "2026-11-23", "data_json": "pas du json"}]
    assert planning_en_cours(liste, date(2026, 11, 29))["id"] == 1
    # ancien planning sans nb_jours : dernier jour porteur d'un plat
    vieux = _p(3, "2026-11-23", plats=[{"jour": 2}])
    assert planning_en_cours([vieux, _p(1, "2020-01-06", 7)], date(2026, 11, 24))["id"] == 3
    assert planning_en_cours([_p(1, "2020-01-06", 7), vieux], date(2026, 11, 24))["id"] == 3


# ── Route ───────────────────────────────────────────────────────────────

def test_semaine_sans_planning_renvoie_au_constructeur(client):
    r = client.get("/semaine", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_semaine_mene_au_planning_en_cours(client):
    today = date.today()
    en_cours = _creer((today - timedelta(days=1)).isoformat(), 7)
    _creer((today + timedelta(days=14)).isoformat(), 7)  # plus récent, à venir
    _creer((today - timedelta(days=1)).isoformat(), 7, valide=False)  # brouillon ignoré
    r = client.get("/semaine", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/planning/{en_cours}"


def test_semaine_repli_sur_le_plus_recent(client):
    today = date.today()
    _creer((today - timedelta(days=60)).isoformat(), 7)
    recent = _creer((today - timedelta(days=30)).isoformat(), 7)
    r = client.get("/semaine", follow_redirects=False)
    assert r.headers["location"] == f"/planning/{recent}"


# ── Menu et page « Données » ────────────────────────────────────────────

def _nav(html: str) -> str:
    return re.search(r'<div class="nav-links">(.*?)</div>', html, re.DOTALL).group(1)


def test_menu_cette_semaine_et_donnees(client):
    nav = _nav(client.get("/").text)
    assert 'href="/semaine"' in nav and ">Cette semaine<" in nav
    assert 'href="/admin"' in nav and ">Données<" in nav
    nav_admin = _nav(client.get("/admin").text)
    assert re.search(r'href="/admin" class="active" aria-current="page">Données<', nav_admin)


def test_bandeau_admin_sans_auth(client):
    assert "Accès non protégé" in client.get("/admin").text
    # pas ailleurs
    assert "Accès non protégé" not in client.get("/historique").text


def test_pas_de_bandeau_quand_auth_active(tmp_path):
    with _client(tmp_path, "chef", "secret") as c:
        c.post("/login", data={"username": "chef", "password": "secret", "next": "/admin"})
        r = c.get("/admin")
        assert r.status_code == 200 and "Accès non protégé" not in r.text
        assert ">Déconnexion<" in _nav(r.text)
    with _client(tmp_path) as c:  # remet l'app sans auth pour les tests suivants
        assert "Accès non protégé" in c.get("/admin").text
