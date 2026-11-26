"""L10 — édition groupée : POST /api/planning/{id}/meals (une requête, une écriture)."""

import json

import pytest
from starlette.testclient import TestClient

from app import main
from app.deps import BASE_DIR
from app.services import planning as planning_svc
from tests.test_brouillons_integrite import _catalogue, _data, _planning
from tests.test_routes import _case, _construire, _recipe


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


def _post(client, pid, **body):
    return client.post(f"/api/planning/{pid}/meals", json=body)


def _plat(pid, jour, moment):
    return next((p for p in _data(pid)["plats"] if p["jour"] == jour and p["moment"] == moment), None)


def _semaine_fusionnee(client, monkeypatch, *extra):
    """Midi des jours 1-3 groupés (même plat = case fusionnée de 3 jours)."""
    poulet, curry, riz = _recipe("Poulet", id="p1"), _recipe("Curry", id="c1"), _recipe("Riz", id="r1", base=["Légume"])
    _catalogue(monkeypatch, poulet, curry, riz, *extra)
    pid = _construire(client, [
        _case(1, "midi", poulet, persons=2, group=1, accs=[riz]),
        _case(2, "midi", None, persons=2, group=1),
        _case(3, "midi", None, persons=2, group=1),
        _case(1, "soir", curry, persons=2),
    ])
    return pid


SLOTS_3J = [{"jour": j, "moment": "midi"} for j in (1, 2, 3)]


def test_remplacer_case_fusionnee_une_seule_ecriture(client, monkeypatch):
    pid = _semaine_fusionnee(client, monkeypatch)
    ecritures = []
    real = main.db.update_planning

    async def _spy(*a, **k):
        ecritures.append(1)
        return await real(*a, **k)
    monkeypatch.setattr(main.db, "update_planning", _spy)

    r = _post(client, pid, slots=SLOTS_3J, action="set_main", nom="Curry")
    assert r.status_code == 200 and r.json()["success"]
    assert len(ecritures) == 1
    for j in (1, 2, 3):
        p = _plat(pid, j, "midi")
        assert p["nom_recette"] == "Curry" and p["notion_id"] == "c1"
        assert p["accompagnements"] == []
    # Les lignes planning_recipes suivent.
    noms = [r["recipe_name"] for r in _planning(pid)["recipes"] if r["moment"] == "midi"]
    assert noms == ["Curry"] * 3


def test_erreur_simulee_aucun_jour_modifie(client, monkeypatch):
    pid = _semaine_fusionnee(client, monkeypatch)
    avant = _planning(pid)["data_json"]

    async def _boom(*a, **k):
        raise RuntimeError("panne disque")
    # L'erreur survient à l'écriture, après les mutations en mémoire.
    monkeypatch.setattr(main.db, "update_planning", _boom)
    r = _post(client, pid, slots=SLOTS_3J, action="set_main", nom="Curry")
    assert r.status_code == 500 and "error" in r.json()
    assert _planning(pid)["data_json"] == avant


def test_erreur_pendant_les_mutations_aucun_jour_modifie(client, monkeypatch):
    pid = _semaine_fusionnee(client, monkeypatch)
    avant = _planning(pid)["data_json"]
    appels = {"n": 0}
    real = planning_svc._set_main

    def _set_main_fragile(*a, **k):
        appels["n"] += 1
        if appels["n"] == 2:   # 2ᵉ jour : échec au milieu du lot
            raise ValueError("boom")
        return real(*a, **k)
    monkeypatch.setattr(planning_svc, "_set_main", _set_main_fragile)
    r = _post(client, pid, slots=SLOTS_3J, action="set_main", nom="Curry")
    assert r.status_code == 500
    assert _planning(pid)["data_json"] == avant


def test_add_side_sur_creneau_vide_refuse_sans_rien_ecrire(client, monkeypatch):
    pid = _semaine_fusionnee(client, monkeypatch)
    avant = _planning(pid)["data_json"]
    r = _post(client, pid, slots=[{"jour": 1, "moment": "midi"}, {"jour": 5, "moment": "soir"}],
              action="add_side", nom="Riz")
    assert r.status_code == 404
    assert _planning(pid)["data_json"] == avant


def test_add_remplace_et_remove_side(client, monkeypatch):
    haricots = _recipe("Haricots", id="h1", base=["Légume"])
    pid = _semaine_fusionnee(client, monkeypatch, haricots)
    # ↻ Changer l'accompagnement : Riz → Haricots, sur les 3 jours.
    r = _post(client, pid, slots=SLOTS_3J, action="add_side", nom="Haricots", remplace="Riz")
    assert r.json()["success"]
    for j in (1, 2, 3):
        assert [a["nom_recette"] for a in _plat(pid, j, "midi")["accompagnements"]] == ["Haricots"]
        assert _plat(pid, j, "midi")["accompagnements"][0]["notion_id"] == "h1"
    # Doublon ignoré.
    _post(client, pid, slots=SLOTS_3J, action="add_side", nom="haricots")
    assert len(_plat(pid, 2, "midi")["accompagnements"]) == 1
    # Accompagnement libre (inconnu du catalogue).
    _post(client, pid, slots=[{"jour": 1, "moment": "soir"}], action="add_side", nom="Salade")
    acc = _plat(pid, 1, "soir")["accompagnements"][0]
    assert acc["libre"] is True
    # Retrait.
    r = _post(client, pid, slots=SLOTS_3J, action="remove_side", nom="Haricots")
    assert r.json()["success"]
    assert all(_plat(pid, j, "midi")["accompagnements"] == [] for j in (1, 2, 3))


def test_set_main_sur_creneau_vide_cree_le_plat(client, monkeypatch):
    """Contrat utilisé par L11 (ajout depuis une fiche recette)."""
    pid = _semaine_fusionnee(client, monkeypatch)
    data = _data(pid)
    data["off_meals"] = ["2:soir"]
    data["nb_jours"] = 3
    import asyncio
    asyncio.run(main.db.update_planning_data(pid, json.dumps(data)))

    r = _post(client, pid, slots=[{"jour": 2, "moment": "soir"}, {"jour": 5, "moment": "soir", "persons": 6}],
              action="set_main", nom="Poulet")
    assert r.json()["success"]
    p2, p5 = _plat(pid, 2, "soir"), _plat(pid, 5, "soir")
    assert p2["notion_id"] == "p1" and p2["persons"] == 2   # convives du jour
    assert p5["persons"] == 6 and p5["libre"] is False
    d = _data(pid)
    assert "2:soir" not in d["off_meals"]
    assert d["nb_jours"] == 5                                # la période s'étend
    assert "builder" in d and any(c.get("main", {}).get("nom") == "Poulet"
                                  for c in d["builder"] if c["jour"] == 5)
    # Les plats restent triés (jour, midi avant soir).
    cles = [(p["jour"], p["moment"] != "midi") for p in d["plats"]]
    assert cles == sorted(cles)
    # Visible sur la page planning.
    assert client.get(f"/planning/{pid}").text.count('class="day-head"') == 5


def test_set_main_nom_inconnu_devient_repas_libre(client, monkeypatch):
    pid = _semaine_fusionnee(client, monkeypatch)
    _post(client, pid, slots=[{"jour": 1, "moment": "soir"}], action="set_main", nom="Pizzas", )
    p = _plat(pid, 1, "soir")
    assert p["nom_recette"] == "Pizzas" and p["libre"] is True and p["notion_id"] == ""


def test_set_main_persons_sur_plat_existant(client, monkeypatch):
    pid = _semaine_fusionnee(client, monkeypatch)
    _post(client, pid, slots=[{"jour": 1, "moment": "soir", "persons": 5}], action="set_main", nom="Poulet")
    assert _plat(pid, 1, "soir")["persons"] == 5


def test_validation_du_corps(client, monkeypatch):
    pid = _semaine_fusionnee(client, monkeypatch)
    avant = _planning(pid)["data_json"]
    mauvais = [
        {},
        {"slots": [], "action": "set_main", "nom": "Curry"},
        {"slots": SLOTS_3J, "action": "explose", "nom": "Curry"},
        {"slots": SLOTS_3J, "action": "set_main", "nom": "  "},
        {"slots": [{"jour": 8, "moment": "midi"}], "action": "set_main", "nom": "Curry"},
        {"slots": [{"jour": True, "moment": "midi"}], "action": "set_main", "nom": "Curry"},
        {"slots": [{"jour": 1, "moment": "matin"}], "action": "set_main", "nom": "Curry"},
        {"slots": ["1:midi"], "action": "set_main", "nom": "Curry"},
        {"slots": [{"jour": 1, "moment": "midi"}] * 15, "action": "set_main", "nom": "Curry"},
    ]
    for body in mauvais:
        r = _post(client, pid, **body)
        assert r.status_code == 400, body
    r = client.post(f"/api/planning/{pid}/meals", content=b"pas du json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert _planning(pid)["data_json"] == avant
    assert _post(client, 99999, slots=SLOTS_3J, action="set_main", nom="Curry").status_code == 404


def test_slots_dedoublonnes():
    slots = planning_svc._parse_slots([{"jour": 1, "moment": "midi"}, {"jour": 1, "moment": "midi", "persons": "x"}])
    assert slots == [{"jour": 1, "moment": "midi", "persons": 0}]


def test_semaine_validee_reste_validee(client, monkeypatch):
    pid = _semaine_fusionnee(client, monkeypatch)
    client.post(f"/planning/{pid}/valider")
    _post(client, pid, slots=SLOTS_3J, action="set_main", nom="Curry")
    assert _planning(pid)["valide"] == 1


def test_planning_week_js_utilise_l_endpoint_groupe():
    js = (BASE_DIR / "static" / "js" / "planning-week.js").read_text()
    assert "/meals`" in js
    assert "/api/update-meal" not in js and "/api/add-side" not in js and "/api/remove-side" not in js
