"""M10/M11 — découpage des services : anciens noms retirés (M11), aucun
import circulaire quel que soit le module chargé en premier, taille des
fichiers, titre de la semaine aligné sur la période, repas libre
« sel, poivre »."""

import asyncio
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

import app.main as main
from app.services import planning, store
from tests.aides import _case, _construire, _recipe

SERVICES = Path(__file__).resolve().parent.parent / "app" / "services"
MODULES = sorted(f"app.services.{p.stem}" for p in SERVICES.glob("*.py") if p.stem != "__init__")


# M11 : les ré-exports des anciens noms (`compat`) sont retirés ; routeurs,
# scripts et tests importent les noms publics depuis leur module actuel.
ANCIENS = {
    "planning": ["_builder_from_plats", "_week_label", "_nb_jours", "_alternatives", "copy_data",
                 "planning_from_builder", "apply_meals_batch", "MEAL_ACTIONS", "GROUPES_MAX"],
    "shopping": ["_collect_shopping", "_ingredients_to_text", "add_extra", "get_placard", "share_action"],
    "recettes": ["_planifier", "_enrich_all_events", "_ajouter", "_usage_label"],
    "store": ["_load"],
}


@pytest.mark.parametrize("module, noms", sorted(ANCIENS.items()))
def test_anciens_noms_retires(module, noms):
    mod = importlib.import_module(f"app.services.{module}")
    for nom in noms:
        assert not hasattr(mod, nom), f"{module}.{nom}"


def test_module_compat_supprime():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.services.compat")
    assert store.load('["x"]') == {} and store.load('{"a": 1}') == {"a": 1}


@pytest.mark.parametrize("module", MODULES)
def test_chaque_module_s_importe_en_premier(module):
    """Pas d'import circulaire : chaque service se charge seul, dans un
    interpréteur neuf, et ses voisins avec lui."""
    code = f"import {module}; import app.main"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=SERVICES.parent.parent, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]


@pytest.mark.parametrize("module", MODULES)
def test_aucun_service_au_dessus_de_350_lignes(module):
    path = SERVICES / f"{module.rsplit('.', 1)[1]}.py"
    assert len(path.read_text(encoding="utf-8").splitlines()) <= 350


# ── Titre de la semaine : suit la période (nb_jours) ───────────────────


def test_titre_suit_la_periode_quand_le_dernier_jour_est_vide():
    plats = [{"jour": 1}, {"jour": 2}]
    # Période de 3 jours, le 3ᵉ vide : la grille montre 3 jours, le titre aussi.
    assert planning.week_label("2026-07-13", plats, 3) == "Lundi 13/07 → mercredi 15/07"
    # Repli (anciens plannings sans période) : dernier jour porteur d'un plat.
    assert planning.week_label("2026-07-13", plats) == "Lundi 13/07 → mardi 14/07"
    assert planning.week_label("2026-07-13", plats, None) == "Lundi 13/07 → mardi 14/07"
    # Période plus courte que le dernier plat, ou invalide : on ne coupe pas.
    assert planning.week_label("2026-07-13", plats, 1) == "Lundi 13/07 → mardi 14/07"
    assert planning.week_label("2026-07-13", plats, "x") == "Lundi 13/07 → mardi 14/07"
    assert planning.week_label("2026-07-13", [{"jour": 1}], 1) == "Lundi 13/07"
    assert planning.week_label("2026-07-13", [], 3) == "Semaine du 2026-07-13"
    assert planning.week_label("bad", plats, 3) == "Semaine du bad"


def test_libelle_des_listes_suit_la_periode():
    p = planning.attach_label({"week_start": "2026-07-13", "data_json": json.dumps(
        {"plats": [{"jour": 1}, {"jour": 2}], "nb_jours": 4})})
    assert p["label"] == "Lundi 13/07 → jeudi 16/07"
    # Ancien planning (sans nb_jours) et data_json illisible : comportement inchangé.
    assert planning.attach_label({"week_start": "2026-07-13", "data_json": json.dumps(
        {"plats": [{"jour": 2}]})})["label"] == "Mardi 14/07"
    assert planning.attach_label({"week_start": "2026-07-13", "data_json": "["})["label"] \
        == "Semaine du 2026-07-13"


def test_historique_affiche_la_periode_complete(client, monkeypatch):
    poulet = _recipe("Poulet", id="p1")

    async def _all(*a, **k):
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    cases = [_case(j, m, main=poulet if j == 1 else None, persons=2 if j <= 3 else 0)
             for j in range(1, 8) for m in ("midi", "soir")]
    pid = _construire(client, cases, week_start="2026-07-13")
    data = json.loads(asyncio.run(main.db.get_planning_with_recipes(pid))["data_json"])
    assert data["nb_jours"] == 3
    assert "Lundi 13/07 → mercredi 15/07" in client.get("/historique").text


# ── Repas libre : « sel, poivre » donne deux ingrédients ────────────────


def test_repas_libre_eclate_les_condiments(client, monkeypatch):
    poulet = _recipe("Poulet", id="p1")

    async def _all(*a, **k):
        return [poulet]

    async def _create(*a, **k):
        return {"id": "libre1", "url": ""}

    async def _noop(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.notion, "create_recipe", _create)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    pid = _construire(client, [_case(1, "midi", main=poulet)])
    r = client.post(f"/api/free-meal/{pid}", json={
        "jour": 1, "moment": "midi", "nom": "Omelette", "ingredients": "3 oeufs\nsel, poivre"})
    assert r.json().get("success")
    cache = asyncio.run(main.db.get_enriched("libre1"))
    assert [i["nom"] for i in json.loads(cache["ingredients"])] == ["oeufs", "sel", "poivre"]


# ── Ajout d'une recette : pas de détail d'exception dans la page ────────


def test_ajout_en_echec_message_generique_detail_dans_les_logs(client, monkeypatch, caplog):
    async def _boom(*a, **k):
        raise RuntimeError("jeton Notion secret-123 refusé")
    monkeypatch.setattr(main.notion, "create_recipe", _boom)
    with caplog.at_level("ERROR"):
        r = client.post("/ajouter-recette", data={"nom": "Gratin", "repas": "Plat"})
    assert "secret-123" not in r.text and "RuntimeError" not in r.text
    assert "Ajout de la recette impossible" in r.text
    assert "Gratin" in r.text      # saisie conservée pour ré-afficher le formulaire
    assert any("secret-123" in (rec.exc_text or "") for rec in caplog.records)
