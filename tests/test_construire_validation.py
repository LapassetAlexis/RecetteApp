"""M02 — /construire validé, une seule implémentation par règle (N8, N11)."""

import asyncio
import json
import re
import sqlite3

import pytest

from app import main
from app.deps import BASE_DIR
from app.services import planning as planning_svc
from app.services import shopping as shopping_svc
from app.services import store
from app.services.recettes import _planifier
from tests.test_brouillons_integrite import _catalogue, _data, _planning
from tests.test_routes import _case, _construire, _recipe, client  # noqa: F401  (fixture)

POULET, CURRY = _recipe("Poulet", id="p1"), _recipe("Curry", id="c1")


def _nb_plannings() -> int:
    """Plannings en base, brouillons compris."""
    with sqlite3.connect(main.db.path) as conn:
        return conn.execute("SELECT COUNT(*) FROM planning_history").fetchone()[0]


def _enrichir():
    asyncio.run(main.db.save_enriched("p1", "Poulet", ingredients=json.dumps(
        [{"nom": "poulet", "quantite": "200", "unite": "g"},
         {"nom": "tomates", "quantite": "2", "unite": ""}])))


# ── week_start ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("saisie", ["pas-une-date", "", "2026-13-01", "26/11/2026"])
def test_construire_date_invalide_ne_cree_rien(client, monkeypatch, saisie):  # noqa: F811
    _catalogue(monkeypatch, POULET)
    cases = [_case(1, "midi", POULET, persons=2)]
    r = client.post("/construire", data={"week_start": saisie, "meals": json.dumps(cases)},
                    follow_redirects=False)
    assert r.status_code == 400
    assert "Date de début invalide" in r.text
    # Saisie conservée : la grille et la date tapée sont ré-affichées.
    assert f'value="{saisie}"' in r.text and "Poulet" in r.text
    assert _nb_plannings() == 0


def test_construire_date_invalide_ne_touche_pas_au_brouillon(client, monkeypatch):  # noqa: F811
    _catalogue(monkeypatch, POULET, CURRY)
    pid = _construire(client, [_case(1, "midi", POULET, persons=2)])
    avant = _planning(pid)
    client.post("/construire", data={
        "week_start": "pas-une-date", "draft_id": str(pid),
        "meals": json.dumps([_case(1, "midi", CURRY, persons=2)])})
    assert _planning(pid)["data_json"] == avant["data_json"]
    assert _planning(pid)["week_start"] == "2026-01-05"


def test_parse_week_start():
    assert planning_svc.parse_week_start(" 2026-11-23 ") == "2026-11-23"
    assert planning_svc.parse_week_start(None) is None
    assert planning_svc.parse_week_start("2026-02-30") is None


# ── Liste de courses et mise à jour d'un brouillon ─────────────────────


def test_construire_ecrit_la_cle_et_le_rayon_de_chaque_article(client, monkeypatch):  # noqa: F811
    _catalogue(monkeypatch, POULET)
    _enrichir()
    pid = _construire(client, [_case(1, "midi", POULET, persons=4)])
    liste = _data(pid)["liste_courses"]
    assert {i["key"] for i in liste} == {"poulet|masse", "tomate|"}
    assert all(i.get("rayon") for i in liste)


def test_construire_sur_un_brouillon_garde_partage_et_articles(client, monkeypatch):  # noqa: F811
    _catalogue(monkeypatch, POULET, CURRY)
    _enrichir()
    pid = _construire(client, [_case(1, "midi", POULET, persons=2)])
    token = client.post(f"/planning/{pid}/partager").json()["url"].rsplit("/", 1)[1]
    client.post(f"/api/courses/{pid}/extra", json={"texte": "café"})
    client.post(f"/api/shopping-check/{pid}", json={"item": "poulet|masse", "checked": True})

    r = client.post("/construire", data={
        "week_start": "2026-02-02", "draft_id": str(pid), "action": "brouillon",
        "meals": json.dumps([_case(1, "midi", POULET, persons=3),
                             _case(1, "soir", CURRY, persons=3)])}, follow_redirects=False)
    assert r.headers["location"] == f"/?draft={pid}&saved=1"
    p, data = _planning(pid), _data(pid)
    assert p["week_start"] == "2026-02-02" and p["nb_personnes"] == 3
    assert data["share_token"] == token and [e["nom"] for e in data["courses_extra"]] == ["café"]
    assert data["courses_checked"] == ["poulet|masse"]
    assert {r["recipe_name"] for r in p["recipes"]} == {"Poulet", "Curry"}
    assert _nb_plannings() == 1


def test_construire_brouillon_supprime_entre_temps_en_cree_un_nouveau(client, monkeypatch):  # noqa: F811
    _catalogue(monkeypatch, POULET)
    pid = _construire(client, [_case(1, "midi", POULET, persons=2)])
    real = store.update_planning

    async def _supprime_puis_ecrit(planning_id, fn, **kw):
        await main.db.delete_planning(planning_id)
        return await real(planning_id, fn, **kw)
    monkeypatch.setattr(store, "update_planning", _supprime_puis_ecrit)
    r = client.post("/construire", data={
        "week_start": "2026-02-02", "draft_id": str(pid),
        "meals": json.dumps([_case(1, "midi", POULET, persons=2)])}, follow_redirects=False)
    new_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert new_id != pid and _planning(new_id)["week_start"] == "2026-02-02"


def test_planning_from_builder_sans_repas(client, monkeypatch):  # noqa: F811
    _catalogue(monkeypatch, POULET)
    assert asyncio.run(planning_svc.planning_from_builder("[]", "")) is None


# ── Routes dépréciées et code mort retirés (décision D2) ───────────────


@pytest.mark.parametrize("route", ["update-meal", "add-side", "remove-side"])
def test_routes_depreciees_supprimees(client, route):  # noqa: F811
    assert client.post(f"/api/{route}/1", json={}).status_code == 404


def test_code_mort_retire():
    assert not hasattr(planning_svc, "_load_slot")
    assert not hasattr(shopping_svc, "_mutate_sides")
    courses = (BASE_DIR / "routers" / "courses.py").read_text()
    assert "ingredients_force" not in courses
    router = (BASE_DIR / "routers" / "planning.py").read_text()
    for cle in ("courses_par_rayon", "rayon_order", "creneaux_non_resolus", "briques_manquantes"):
        assert f'"{cle}"' not in router


def test_router_planning_moins_de_350_lignes():
    assert len((BASE_DIR / "routers" / "planning.py").read_text().splitlines()) < 350


def test_generation_ignore_ingredients_force(client, monkeypatch):  # noqa: F811
    _catalogue(monkeypatch, POULET)
    _enrichir()
    pid = asyncio.run(main.db.save_planning(
        week_start="2026-01-05", saison="", nb_personnes=4, ingredients_force="sel, beurre",
        data_json=json.dumps({"plats": [{"jour": 1, "moment": "midi", "nom_recette": "Poulet",
                                         "notion_id": "p1", "persons": 4}]}),
        recipes=[]))
    noms = {i["nom"] for i in client.post(f"/generate-shopping/{pid}").json()["liste_courses"]}
    assert noms == {"poulet", "tomates"}


def test_generation_planning_vide_ou_absent(client, monkeypatch):  # noqa: F811
    pid = asyncio.run(main.db.save_planning(
        week_start="2026-01-05", saison="", nb_personnes=4, ingredients_force="",
        data_json=json.dumps({"plats": []}), recipes=[]))
    avant = _planning(pid)["data_json"]
    assert client.post(f"/generate-shopping/{pid}").json() == {"error": "Aucun plat dans ce planning"}
    assert _planning(pid)["data_json"] == avant
    assert client.post("/generate-shopping/9999").json() == {"error": "Planning introuvable"}
    assert client.post("/planning/9999/partager").json() == {"error": "Planning introuvable"}


# ── Dupliquer et aperçu (fonctions sorties du routeur) ─────────────────


def test_dupliquer_et_apercu(client, monkeypatch):  # noqa: F811
    _catalogue(monkeypatch, POULET, CURRY)
    pid = _construire(client, [_case(1, "midi", POULET, persons=2),
                               _case(2, "soir", CURRY, persons=2)])
    new_id = client.post(f"/planning/{pid}/dupliquer").json()["planning_id"]
    data = _data(new_id)
    assert data["liste_courses"] == [] and len(data["plats"]) == 2 and data["builder"]
    jours = client.get(f"/api/planning/{pid}/apercu").json()["jours"]
    assert [(j["midi"], j["soir"]) for j in jours] == [("Poulet", ""), ("", "Curry")]


def test_copy_data_reconstruit_la_grille_des_anciens_plannings():
    data = planning_svc.copy_data({"plats": [{"jour": 1, "moment": "midi", "nom_recette": "A"}],
                                   "nb_jours": 2})
    assert len(data["builder"]) == 14 and data["nb_jours"] == 2


# ── Ajout depuis une fiche : même règle que set_main ───────────────────


def _fiche(monkeypatch):
    async def _get(page_id):
        return CURRY if page_id == "c1" else None
    monkeypatch.setattr(main.notion, "get_recipe", _get)
    _catalogue(monkeypatch, POULET, CURRY)


def test_planifier_nouveau_brouillon_supprime_si_l_ecriture_echoue(client, monkeypatch):  # noqa: F811
    _fiche(monkeypatch)

    async def _boom(*a, **k):
        raise RuntimeError("disque plein")
    monkeypatch.setattr(store, "update_planning", _boom)
    with pytest.raises(RuntimeError):
        asyncio.run(_planifier("c1", {"moment": "soir", "date": "2026-03-02"}))
    assert _nb_plannings() == 0


def test_planifier_parametres_invalides_ne_cree_rien(client, monkeypatch):  # noqa: F811
    _fiche(monkeypatch)
    for body in ({"moment": "goûter"}, {"moment": "soir", "date": "pas-une-date"},
                 {"moment": "soir", "planning_id": 1, "jour": 9}):
        assert "error" in asyncio.run(_planifier("c1", body))
    assert "error" in asyncio.run(_planifier("inconnue", {"moment": "soir"}))
    assert _nb_plannings() == 0


def test_planifier_semaine_validee_refusee(client, monkeypatch):  # noqa: F811
    _fiche(monkeypatch)
    pid = _construire(client, [_case(1, "midi", POULET, persons=2)])
    asyncio.run(main.db.mark_planning_valid(pid))
    avant = _planning(pid)["data_json"]
    r = asyncio.run(_planifier("c1", {"planning_id": pid, "jour": 1, "moment": "soir"}))
    assert r == {"error": "Brouillon introuvable"}
    assert _planning(pid)["data_json"] == avant
    assert asyncio.run(_planifier("c1", {"planning_id": 999, "jour": 1, "moment": "soir"})) \
        == {"error": "Brouillon introuvable"}


def test_planifier_remplace_avec_les_convives_demandes(client, monkeypatch):  # noqa: F811
    _fiche(monkeypatch)
    pid = _construire(client, [_case(1, "midi", POULET, persons=2)])
    r = asyncio.run(_planifier("c1", {"planning_id": pid, "jour": 1, "moment": "midi",
                                          "persons": 5}))
    assert r["success"] and r["draft"]["occupied"]["1:midi"] == "Curry"
    plat = _data(pid)["plats"][0]
    assert (plat["nom_recette"], plat["persons"], plat["libre"]) == ("Curry", 5, False)
    assert [x["recipe_name"] for x in _planning(pid)["recipes"]] == ["Curry"]


def test_aucun_appel_direct_a_l_ecriture_du_data_json_hors_store():
    """Les services et routeurs n'écrivent plus le data_json eux-mêmes."""
    for path in list((BASE_DIR / "services").glob("*.py")) + list((BASE_DIR / "routers").glob("*.py")):
        if path.name in ("store.py", "backup.py"):
            continue
        src = path.read_text()
        assert not re.search(r"db\.update_planning(_data)?\(", src), path.name
