"""L11 — « Ajouter au planning » depuis une fiche recette :
POST /api/recette/{id}/planifier (brouillon existant ou nouveau)."""

import asyncio
import json
from datetime import date

import app.main as main
from app.services import recette_planifier
from tests.test_routes import _case, _construire, _recipe, client  # noqa: F401  (fixture réutilisée)

CURRY = _recipe("Curry", id="curry", base=["Viande"])
TARTE = _recipe("Tarte", "Dessert", id="tarte")
RIZ = _recipe("Riz", id="riz")


def _mock_catalogue(monkeypatch):
    by_id = {r["id"]: r for r in (CURRY, TARTE, RIZ)}

    async def _all():
        return list(by_id.values())

    async def _get(pid):
        return by_id.get(pid)

    async def _enriched(nid):
        if nid == "curry":
            return {"ingredients": json.dumps([{"nom": "poulet", "quantite": "400", "unite": "g"}]),
                    "base_servings": 4}
        return None
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.notion, "get_recipe", _get)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)


def _data(pid):
    p = asyncio.run(main.db.get_planning_with_recipes(pid))
    return p, json.loads(p["data_json"])


def _post(c, rid, body):
    return c.post(f"/api/recette/{rid}/planifier", json=body).json()


def test_ajout_dans_un_creneau_vide_du_brouillon(client, monkeypatch):  # noqa: F811
    _mock_catalogue(monkeypatch)
    pid = _construire(client, [_case(1, "midi", main=TARTE, persons=2), _case(2, "soir", persons=3)],
                      week_start="2026-03-02")
    r = _post(client, "curry", {"planning_id": pid, "jour": 2, "moment": "soir"})
    assert r["success"] and r["planning_id"] == pid and r["url"] == f"/?draft={pid}"
    assert r["draft"]["occupied"]["2:soir"] == "Curry"
    planning, data = _data(pid)
    plat = next(p for p in data["plats"] if (p["jour"], p["moment"]) == (2, "soir"))
    assert plat["nom_recette"] == "Curry" and plat["notion_id"] == "curry" and plat["base"] == ["Viande"]
    assert [(p["jour"], p["moment"]) for p in data["plats"]] == [(1, "midi"), (2, "soir")]
    # Liste de courses régénérée, grille du constructeur à jour, table des recettes aussi.
    assert any(it["nom"] == "poulet" for it in data["liste_courses"])
    case = next(c for c in data["builder"] if (c["jour"], c["moment"]) == (2, "soir"))
    assert case["main"]["notion_id"] == "curry"
    assert {r["recipe_name"] for r in planning["recipes"]} == {"Tarte", "Curry"}
    assert not planning["valide"]


def test_ajout_remplace_le_plat_du_creneau(client, monkeypatch):  # noqa: F811
    _mock_catalogue(monkeypatch)
    pid = _construire(client, [_case(1, "midi", main=TARTE, accs=[RIZ])])
    r = _post(client, "curry", {"planning_id": str(pid), "jour": "1", "moment": "midi"})
    assert r["success"]
    _, data = _data(pid)
    assert len(data["plats"]) == 1
    plat = data["plats"][0]
    assert plat["nom_recette"] == "Curry" and plat["accompagnements"] == [] and not plat["libre"]


def test_ajout_retire_le_creneau_des_absents_et_etend_la_periode(client, monkeypatch):  # noqa: F811
    _mock_catalogue(monkeypatch)
    pid = _construire(client, [_case(1, "midi", main=TARTE), _case(3, "soir", persons=0)])
    planning, data = _data(pid)
    data["nb_jours"] = 1
    asyncio.run(main.db.update_planning_data(pid, json.dumps(data)))
    assert _post(client, "curry", {"planning_id": pid, "jour": 3, "moment": "soir"})["success"]
    _, data = _data(pid)
    assert "3:soir" not in data["off_meals"] and data["nb_jours"] == 3


def test_nouveau_brouillon_d_un_jour(client, monkeypatch):  # noqa: F811
    _mock_catalogue(monkeypatch)
    r = _post(client, "curry", {"planning_id": "new", "date": "2026-04-10", "moment": "soir"})
    assert r["success"]
    planning, data = _data(r["planning_id"])
    assert planning["week_start"] == "2026-04-10" and not planning["valide"]
    assert [(p["jour"], p["moment"], p["nom_recette"]) for p in data["plats"]] == [(1, "soir", "Curry")]
    assert data["nb_jours"] == 1 and data["liste_courses"]
    # Un seul jour : les jours 2 à 7 sont absents dans la grille.
    assert all(c["persons"] == 0 for c in data["builder"] if c["jour"] > 1)
    assert r["draft"]["label"].startswith("Vendredi 10/04")
    drafts = asyncio.run(main.db.list_drafts())
    assert [d["id"] for d in drafts] == [r["planning_id"]]


def test_nouveau_brouillon_date_par_defaut(client, monkeypatch):  # noqa: F811
    _mock_catalogue(monkeypatch)
    r = _post(client, "curry", {"moment": "midi"})
    planning, _ = _data(r["planning_id"])
    assert planning["week_start"] == date.today().isoformat()


def test_erreurs(client, monkeypatch):  # noqa: F811
    _mock_catalogue(monkeypatch)
    pid = _construire(client, [_case(1, "midi", main=TARTE)])
    assert "Moment" in _post(client, "curry", {"planning_id": pid, "jour": 1, "moment": "nuit"})["error"]
    assert _post(client, "curry", {"planning_id": pid, "jour": 9, "moment": "midi"})["error"]
    assert _post(client, "curry", {"planning_id": pid, "jour": "x", "moment": "midi"})["error"]
    assert _post(client, "curry", {"planning_id": "new", "date": "demain", "moment": "midi"})["error"]
    assert _post(client, "inconnue", {"planning_id": pid, "jour": 1, "moment": "midi"})["error"]
    assert _post(client, "curry", {"planning_id": 9999, "jour": 1, "moment": "midi"})["error"]
    # Un planning validé n'est pas un brouillon.
    asyncio.run(main.db.mark_planning_valid(pid))
    assert "Brouillon" in _post(client, "curry", {"planning_id": pid, "jour": 1, "moment": "midi"})["error"]
    assert client.post("/api/recette/curry/planifier", content=b"[]",
                       headers={"Content-Type": "application/json"}).json()["error"]
    assert client.post("/api/recette/curry/planifier", content=b"pas du json").json()["error"]


def test_erreur_inattendue_renvoyee_en_json(client, monkeypatch):  # noqa: F811
    async def _ko(pid):
        raise RuntimeError("Notion HS")
    monkeypatch.setattr(main.notion, "get_recipe", _ko)
    r = client.post("/api/recette/curry/planifier", json={"moment": "midi"})
    # Statut 500 et message générique : le texte de l'exception reste dans les logs (M09).
    assert r.status_code == 500 and r.json()["error"] and "Notion HS" not in r.text


def test_fiche_liste_les_brouillons(client, monkeypatch):  # noqa: F811
    _mock_catalogue(monkeypatch)

    async def _instr(pid):
        return []
    monkeypatch.setattr(main.notion, "get_recipe_instructions", _instr)
    pid = _construire(client, [_case(2, "soir", main=TARTE)], week_start="2026-03-02")
    r = client.get("/recette/curry")
    assert r.status_code == 200
    page_data = json.loads(r.text.split('id="page-data">', 1)[1].split("</script>", 1)[0])
    assert page_data["recette_id"] == "curry"
    (d,) = page_data["drafts"]
    assert d["id"] == pid and d["occupied"] == {"2:soir": "Tarte"}
    assert d["days"][0] == {"jour": 1, "label": "Lundi 02/03"}
    assert d["label"] == "Mardi 03/03"


def test_draft_choices_tolere_un_json_illisible():
    (d,) = recette_planifier.draft_choices([{"id": 1, "week_start": "x", "data_json": "{"}])
    assert d["occupied"] == {} and d["days"][0]["label"] == "Jour 1"
