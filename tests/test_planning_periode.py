"""L10 — période du planning : seuls les jours 1..nb_jours sont affichés."""

import json
import re

import pytest
from starlette.testclient import TestClient

from app import main
from app.services.planning import _nb_jours, _nb_jours_from_cases
from tests.test_brouillons_integrite import _catalogue, _data
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


def _cases_periode(n, poulet):
    """Grille telle que l'envoie le constructeur pour une période de n jours :
    jours hors période « absents » (persons 0)."""
    cases = []
    for j in range(1, 8):
        for m in ("midi", "soir"):
            if j <= n:
                cases.append(_case(j, m, poulet if m == "soir" or j == 1 else None, persons=2))
            else:
                cases.append(_case(j, m, None, persons=0))
    return cases


def _day_heads(html):
    return re.findall(r'class="day-head"', html)


def test_planning_4_jours_affiche_4_colonnes(client, monkeypatch):
    poulet = _recipe("Poulet", id="p1")
    _catalogue(monkeypatch, poulet)
    pid = _construire(client, _cases_periode(4, poulet))
    assert _data(pid)["nb_jours"] == 4

    html = client.get(f"/planning/{pid}").text
    assert len(_day_heads(html)) == 4
    assert "--nb-jours:4" in html
    assert "/static/css/planning.css" in html
    # Les jours 5-7 (hors période) ne sont pas « Absent » : ils sont masqués.
    assert "Absent" not in html


def test_absent_reste_pour_un_creneau_a_zero_dans_la_periode(client, monkeypatch):
    poulet = _recipe("Poulet", id="p1")
    _catalogue(monkeypatch, poulet)
    cases = _cases_periode(3, poulet)
    # Jour 2 midi : 0 convive, dans la période → « Absent ».
    for c in cases:
        if c["jour"] == 2 and c["moment"] == "midi":
            c["persons"] = 0
    pid = _construire(client, cases)
    html = client.get(f"/planning/{pid}").text
    assert len(_day_heads(html)) == 3
    # Une case grisée dans la grille + une dans la liste mobile.
    assert html.count("Absent") == 2


def test_ancien_planning_sans_nb_jours_repli_dernier_plat(client, monkeypatch):
    _catalogue(monkeypatch)
    data = {"plats": [
        {"jour": 1, "moment": "midi", "nom_recette": "A", "persons": 2},
        {"jour": 5, "moment": "soir", "nom_recette": "B", "persons": 2},
    ], "liste_courses": []}
    import asyncio
    pid = asyncio.run(main.db.save_planning("2026-01-05", "", 2, "", json.dumps(data), []))
    html = client.get(f"/planning/{pid}").text
    assert len(_day_heads(html)) == 5


def test_nb_jours_regles():
    assert _nb_jours({}) == 7
    assert _nb_jours({"plats": [{"jour": 3}]}) == 3
    assert _nb_jours({"nb_jours": 2, "plats": [{"jour": 4}]}) == 4   # jamais sous un plat
    assert _nb_jours({"nb_jours": 6, "plats": [{"jour": 2}]}) == 6
    assert _nb_jours({"nb_jours": "x", "plats": [{"jour": "?"}]}) == 7
    assert _nb_jours({"nb_jours": 12}) == 7
    assert _nb_jours_from_cases([{"jour": 3, "persons": 2}, {"jour": 6, "persons": 0}]) == 3
    assert _nb_jours_from_cases([]) == 0


def test_dupliquer_garde_la_periode(client, monkeypatch):
    poulet = _recipe("Poulet", id="p1")
    _catalogue(monkeypatch, poulet)
    pid = _construire(client, _cases_periode(3, poulet))
    new_id = client.post(f"/planning/{pid}/dupliquer").json()["planning_id"]
    assert _data(new_id)["nb_jours"] == 3
