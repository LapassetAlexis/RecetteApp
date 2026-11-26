"""L17 — correctifs finaux : création groupée (repas libre, brique) sur une
case fusionnée, période explicite du constructeur, message d'enregistrement
d'une semaine validée, virgule décimale (fiche, mode cuisine), méta PWA,
date de la copie « Refaire » indépendante du jour."""

import json
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from app import main
from app.services.creations import slots_from_body
from app.services.planning_construire import periode_saisie
from tests.aides import _case, _construire, _recipe
from tests.test_brouillons_integrite import _catalogue, _data, _planning

STATIC = Path(main.__file__).resolve().parent / "static"


@pytest.fixture(autouse=True)
def _ingredients_notion_ignores(monkeypatch):
    async def _noop(*a, **k):
        return True
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)


@pytest.fixture
def notion_cree(monkeypatch):
    """Capture les recettes créées et archivées dans Notion."""
    appels = {"create": [], "archive": []}

    async def _create(nom, nature="Recette", base=None, **kw):
        appels["create"].append({"nom": nom, "nature": nature, "base": base})
        return {"id": f"new{len(appels['create'])}", "url": "https://notion.so/new"}

    async def _archive(page_id):
        appels["archive"].append(page_id)
        return {}
    monkeypatch.setattr(main.notion, "create_recipe", _create)
    monkeypatch.setattr(main.notion, "archive_recipe", _archive)
    return appels


def _semaine_fusionnee(client, monkeypatch) -> int:
    """Midis des jours 1 et 2 groupés (même plat), soir du jour 1."""
    poulet, soupe = _recipe("Poulet", id="p1"), _recipe("Soupe", id="s1")
    _catalogue(monkeypatch, poulet, soupe)
    return _construire(client, [_case(1, "midi", poulet, persons=2, group=1),
                                _case(2, "midi", None, persons=2, group=1),
                                _case(1, "soir", soupe, persons=2)])


def _plat(data, jour, moment):
    return next(p for p in data["plats"] if p["jour"] == jour and p["moment"] == moment)


# ── Repas libre / brique sur une case fusionnée : un appel, une recette ──


def test_repas_libre_plusieurs_creneaux_une_recette(client, monkeypatch, notion_cree):
    pid = _semaine_fusionnee(client, monkeypatch)
    r = client.post(f"/api/free-meal/{pid}", json={
        "slots": [{"jour": 1, "moment": "midi"}, {"jour": 2, "moment": "midi"}],
        "nom": "Steak frites", "ingredients": "2 steaks\n500 g de pommes de terre",
    }).json()
    assert r["success"]
    assert [c["nom"] for c in notion_cree["create"]] == ["Steak frites"]   # une seule recette
    data = _data(pid)
    for jour in (1, 2):
        plat = _plat(data, jour, "midi")
        assert plat["nom_recette"] == "Steak frites" and plat["notion_id"] == "new1"
        assert plat["libre"] is False
    assert _plat(data, 1, "soir")["nom_recette"] == "Soupe"
    assert "steaks" in {i["nom"] for i in data["liste_courses"]}


def test_brique_plusieurs_creneaux_plat_et_accompagnement(client, monkeypatch, notion_cree):
    pid = _semaine_fusionnee(client, monkeypatch)
    slots = [{"jour": 1, "moment": "midi"}, {"jour": 2, "moment": "midi"}]
    r = client.post(f"/api/brique/{pid}", json={
        "slots": slots, "slot": "plat", "nom": "Steak haché", "base": ["Viande"],
    }).json()
    assert r["success"]
    r = client.post(f"/api/brique/{pid}", json={
        "slots": slots, "slot": "accompagnement", "nom": "Riz", "base": "Féculent",
        "quantite": "300", "unite": "g",
    }).json()
    assert r["success"]
    assert [(c["nom"], c["nature"]) for c in notion_cree["create"]] == [
        ("Steak haché", "Ingrédient"), ("Riz", "Ingrédient")]
    data = _data(pid)
    for jour in (1, 2):
        plat = _plat(data, jour, "midi")
        assert plat["nom_recette"] == "Steak haché" and plat["notion_id"] == "new1"
        assert [a["nom_recette"] for a in plat["accompagnements"]] == ["Riz"]


@pytest.mark.parametrize("url, extra", [
    ("free-meal", {"nom": "Steak", "ingredients": ""}),
    ("brique", {"nom": "Steak", "slot": "plat", "base": ["Viande"]}),
])
def test_creneau_manquant_rien_cree_ni_ecrit(client, monkeypatch, notion_cree, url, extra):
    """Un créneau absent du planning : aucune recette Notion, planning intact."""
    pid = _semaine_fusionnee(client, monkeypatch)
    avant = _planning(pid)["data_json"]
    r = client.post(f"/api/{url}/{pid}", json={
        "slots": [{"jour": 1, "moment": "midi"}, {"jour": 3, "moment": "soir"}], **extra,
    }).json()
    assert r.get("error") == "Repas non trouvé dans le planning"
    assert notion_cree["create"] == []
    assert _planning(pid)["data_json"] == avant


def test_erreur_apres_creation_archive_et_n_ecrit_rien(client, monkeypatch, notion_cree):
    """Échec de l'enregistrement après la création Notion : la recette est
    archivée (pas d'orpheline) et le planning n'est pas modifié."""
    pid = _semaine_fusionnee(client, monkeypatch)
    avant = _planning(pid)["data_json"]

    async def _boom(*a, **k):
        raise RuntimeError("disque plein")
    monkeypatch.setattr(main.db, "update_planning_data", _boom)
    monkeypatch.setattr(main.db, "update_planning", _boom)
    r = client.post(f"/api/free-meal/{pid}", json={
        "slots": [{"jour": 1, "moment": "midi"}, {"jour": 2, "moment": "midi"}], "nom": "Steak",
    }).json()
    assert "error" in r
    assert notion_cree["archive"] == ["new1"]
    assert _planning(pid)["data_json"] == avant


def test_appel_unitaire_toujours_accepte(client, monkeypatch, notion_cree):
    pid = _semaine_fusionnee(client, monkeypatch)
    r = client.post(f"/api/free-meal/{pid}", json={"jour": 1, "moment": "soir", "nom": "Omelette"}).json()
    assert r["success"]
    assert _plat(_data(pid), 1, "soir")["nom_recette"] == "Omelette"


@pytest.mark.parametrize("body, attendu", [
    ({"jour": 2, "moment": "soir"}, [(2, "soir")]),
    ({"slots": [{"jour": 1, "moment": "midi"}, {"jour": 1, "moment": "midi"}]}, [(1, "midi")]),
    ({"slots": []}, None),
    ({"slots": [{"jour": 8, "moment": "midi"}]}, None),
    ({"jour": True, "moment": "midi"}, None),
    ({"jour": 1, "moment": "nuit"}, None),
])
def test_slots_from_body(body, attendu):
    assert slots_from_body(body) == attendu


# ── Période explicite envoyée par le constructeur ────────────────────────


def _cases_4_jours_dernier_absent(soupe):
    cases = []
    for j in range(1, 8):
        for m in ("midi", "soir"):
            persons = 2 if j <= 3 else 0
            cases.append(_case(j, m, soupe if (j, m) == (1, "soir") else None, persons=persons))
    return cases


def test_periode_explicite_garde_le_dernier_jour_absent(client, monkeypatch):
    soupe = _recipe("Soupe", id="s1")
    _catalogue(monkeypatch, soupe)
    loc = client.post("/construire", data={
        "week_start": "2026-12-02", "meals": json.dumps(_cases_4_jours_dernier_absent(soupe)),
        "action": "brouillon", "nb_jours": "4",
    }, follow_redirects=False).headers["location"]
    pid = int(re.search(r"draft=(\d+)", loc).group(1))
    assert _data(pid)["nb_jours"] == 4
    page = json.loads(re.search(r'id="page-data">(.*?)</script>',
                                client.get(f"/?draft={pid}").text, re.DOTALL).group(1))
    assert page["nb_jours"] == 4 and page["valide"] is False
    assert client.get(f"/planning/{pid}").text.count('class="day-head"') == 4


def test_periode_absente_ou_invalide_repli_sur_les_cases(client, monkeypatch):
    soupe = _recipe("Soupe", id="s1")
    _catalogue(monkeypatch, soupe)
    for brut in ("", "9", "abc"):
        loc = client.post("/construire", data={
            "week_start": "2026-12-02", "meals": json.dumps(_cases_4_jours_dernier_absent(soupe)),
            "nb_jours": brut,
        }, follow_redirects=False).headers["location"]
        assert _data(int(loc.rsplit("/", 1)[1]))["nb_jours"] == 3


@pytest.mark.parametrize("brut, attendu", [("4", 4), (" 7 ", 7), ("0", 0), ("8", 0), ("", 0), (None, 0)])
def test_periode_saisie(brut, attendu):
    assert periode_saisie(brut) == attendu


# ── Semaine validée rouverte : message adapté ─────────────────────────────


def test_semaine_validee_rouverte_signale_valide(client, monkeypatch):
    pid = _semaine_fusionnee(client, monkeypatch)
    client.post(f"/planning/{pid}/valider")
    page = json.loads(re.search(r'id="page-data">(.*?)</script>',
                                client.get(f"/?planning={pid}&saved=1").text, re.DOTALL).group(1))
    assert page["valide"] is True and page["saved"] is True
    src = (STATIC / "js" / "builder.js").read_text(encoding="utf-8")
    assert "DATA.valide ? 'Semaine mise à jour ✓' : 'Brouillon enregistré ✓'" in src


# ── « Refaire » : copie datée du jour, même un mercredi ───────────────────


def test_refaire_date_du_jour_independante_du_lundi(client, monkeypatch):
    import app.routers.planning as routeur

    class _Mercredi(date):
        @classmethod
        def today(cls):
            return date(2026, 12, 2)
    monkeypatch.setattr(routeur, "date", _Mercredi)
    pid = _semaine_fusionnee(client, monkeypatch)
    copie = client.post(f"/planning/{pid}/dupliquer").json()["planning_id"]
    assert _planning(copie)["week_start"] == "2026-12-02"   # pas le lundi 30/11


# ── Virgule décimale : mode cuisine, fiche recette ───────────────────────


def test_mode_cuisine_virgule_decimale(client, monkeypatch):
    async def _get(pid):
        return _recipe("Rôti", id="r")

    async def _instr(pid):
        return ["Cuire."]

    async def _enriched(pid):
        return {"base_servings": 4, "ingredients": json.dumps([
            {"nom": "bœuf", "quantite": "0.75", "unite": "kg"},
            {"nom": "beurre", "quantite": "12.5", "unite": "g"},
        ])}
    monkeypatch.setattr(main.notion, "get_recipe", _get)
    monkeypatch.setattr(main.notion, "get_recipe_instructions", _instr)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)
    html = client.get("/recette/r/cuisine?portions=8").text
    assert "1,5 kg" in html and "25 g" in html
    assert "1.5 kg" not in html
    assert "0,75 kg" in client.get("/recette/r/cuisine").text


def test_fiche_recette_affiche_via_qtyfr():
    # JS de la fiche sorti dans js/recette-fiche.js (M08).
    js = (Path(main.__file__).resolve().parent / "static" / "js" / "recette-fiche.js").read_text(encoding="utf-8")
    assert "qtyFr(scaled" in js


NODE_QTY = r"""
global.window = {};
global.document = {getElementById: () => null};
global.navigator = {};
require(process.argv[1]);
const cas = JSON.parse(process.argv[2]);
console.log(JSON.stringify(cas.map(v => window.Common.qtyFr(v))));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="Node absent")
def test_qtyfr_node():
    cas = [1.5, 2, 0.333333, 12.5, "1.5", "1 à 1.5", "une pincée", "", None, "v1.2"]
    out = subprocess.run(["node", "-e", NODE_QTY, str(STATIC / "js" / "common.js"), json.dumps(cas)],
                         capture_output=True, text=True, check=True, timeout=30).stdout
    assert json.loads(out) == ["1,5", "2", "0,33", "12,5", "1,5", "1 à 1,5", "une pincée", "", "", "v1,2"]


# ── Méta PWA ───────────────────────────────────────────────────────────────


def test_meta_mobile_web_app_capable(client):
    html = client.get("/historique").text
    assert '<meta name="mobile-web-app-capable" content="yes">' in html
    assert '<meta name="apple-mobile-web-app-capable" content="yes">' in html
