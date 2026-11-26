"""M02 — groupes de midis du constructeur conservés hors constructeur (N3)."""

import itertools
import json
import re

import pytest
from starlette.testclient import TestClient

from app import main
from app.services.planning_builder import GROUPES_MAX, builder_from_plats
from tests.test_brouillons_integrite import _catalogue, _data
from tests.test_routes import _case, _recipe

POULET, CURRY, SOUPE = _recipe("Poulet", id="p1"), _recipe("Curry", id="c1"), _recipe("Soupe", id="s1")
QUICHE, RIZ = _recipe("Quiche", id="q1"), _recipe("Riz", id="r1")


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbpath = str(tmp_path / "t.db")
    main.db.path = dbpath
    monkeypatch.setattr(main.settings, "database_path", dbpath)

    async def _noop(*a, **k):
        return True
    monkeypatch.setattr(main.notion, "ensure_ingredients_field", _noop)
    _catalogue(monkeypatch, POULET, CURRY, SOUPE, QUICHE, RIZ)
    with TestClient(main.app) as c:
        yield c


def _brouillon(client, cases, nb_jours="7") -> int:
    loc = client.post("/construire", data={
        "week_start": "2026-11-23", "meals": json.dumps(cases),
        "action": "brouillon", "nb_jours": nb_jours,
    }, follow_redirects=False).headers["location"]
    return int(re.search(r"draft=(\d+)", loc).group(1))


def _midis(builder) -> dict[int, dict]:
    return {c["jour"]: c for c in builder if c["moment"] == "midi"}


def _set_main(client, pid, jour, moment, nom):
    r = client.post(f"/api/planning/{pid}/meals", json={
        "slots": [{"jour": jour, "moment": moment}], "action": "set_main", "nom": nom})
    assert r.json().get("success"), r.text
    return _data(pid)["builder"]


# ── Critère d'acceptation : G1 / G1 / G4 conservés ──────────────────────


def test_groupes_non_consecutifs_gardes_apres_set_main_sur_un_soir(client):
    pid = _brouillon(client, [
        _case(1, "midi", POULET, persons=2, group=1),
        _case(2, "midi", QUICHE, persons=2, group=2),
        _case(3, "midi", None, persons=2, group=1),       # suit le groupe 1
        _case(4, "midi", CURRY, persons=3, group=4),
        _case(1, "soir", SOUPE, persons=2),
    ], nb_jours="4")
    builder = _set_main(client, pid, 1, "soir", "Riz")
    m = _midis(builder)
    assert (m[1]["group"], m[3]["group"], m[4]["group"]) == (1, 1, 4)
    assert m[2]["group"] == 2
    # Seul le premier midi du groupe porte le repas ; convives gardés.
    assert m[1]["main"]["nom"] == "Poulet" and "main" not in m[3]
    assert m[4]["persons"] == 3
    soir1 = next(c for c in builder if c["jour"] == 1 and c["moment"] == "soir")
    assert soir1["main"]["nom"] == "Riz"


def test_midi_modifie_prend_le_plus_petit_groupe_libre(client):
    pid = _brouillon(client, [
        _case(1, "midi", POULET, persons=2, group=1),
        _case(2, "midi", None, persons=2, group=1),
        _case(3, "midi", CURRY, persons=2, group=3),
    ], nb_jours="3")
    # Le mardi quitte le groupe 1 (nouveau repas) : groupe 2, libre.
    m = _midis(_set_main(client, pid, 2, "midi", "Quiche"))
    assert (m[1]["group"], m[2]["group"], m[3]["group"]) == (1, 2, 3)
    assert m[2]["main"]["nom"] == "Quiche"


def test_midi_modifie_ne_prend_pas_un_groupe_garde_plus_loin(client):
    pid = _brouillon(client, [
        _case(1, "midi", POULET, persons=2, group=2),
        _case(2, "midi", CURRY, persons=2, group=1),
    ], nb_jours="2")
    # Lundi change : le groupe 1 est gardé par mardi, lundi prend le 3.
    m = _midis(_set_main(client, pid, 1, "midi", "Quiche"))
    assert m[2]["group"] == 1
    assert m[1]["group"] not in (1, m[2]["group"]) and m[1]["group"] <= GROUPES_MAX


# ── Sans ancienne grille : numérotation compacte ───────────────────────


def _plat(jour, nom, moment="midi", persons=2):
    return {"jour": jour, "moment": moment, "nom_recette": nom, "notion_id": nom.lower(),
            "persons": persons, "accompagnements": []}


def test_sans_grille_numerotation_compacte():
    plats = [_plat(1, "A"), _plat(2, "A"), _plat(3, "B"), _plat(5, "C"), _plat(7, "D")]
    m = _midis(builder_from_plats(plats))
    assert [m[j]["group"] for j in (1, 2, 3, 5, 7)] == [1, 1, 2, 3, 4]


def _verifie(repas: str) -> None:
    """Sept midis (lettre = repas, espace = midi vide de la période) : aucun
    groupe ne mélange deux repas, et on ne dépasse GROUPES_MAX que si un groupe
    par repas distinct (+ un pour les midis vides) ne tient pas."""
    plats = [_plat(j, r) for j, r in enumerate(repas, start=1) if r != " "]
    m = _midis(builder_from_plats(plats, nb_jours=7))
    besoin = len(set(repas) - {" "}) + (" " in repas)
    if besoin <= GROUPES_MAX:
        assert max(c["group"] for c in m.values()) <= GROUPES_MAX, repas
    par_groupe: dict[int, set] = {}
    for j in range(1, 8):
        par_groupe.setdefault(m[j]["group"], set()).add(repas[j - 1])
    assert all(len(v) == 1 for v in par_groupe.values()), repas


@pytest.mark.parametrize("repas", [
    "AAAAAAA", "ABCDEAB", "ABCABCA", "ABABABA", "ABCDDEE", "AAB CC ", "  A B  ", "ABCDE  ",
])
def test_sept_jours_jamais_au_dela_de_cinq_quand_cinq_suffisent(repas):
    _verifie(repas)


def test_tous_les_plannings_de_sept_jours_restent_coherents():
    for combo in itertools.product("ABC ", repeat=7):
        _verifie("".join(combo))


def test_ancienne_grille_aux_groupes_six_et_sept_renumerotee():
    plats = [_plat(j, n) for j, n in enumerate("ABCDEFF", start=1)]
    previous = builder_from_plats(plats)
    for c in previous:     # grille produite par l'ancienne numérotation (group = jour)
        if c["moment"] == "midi":
            c["group"] = 6 if c["jour"] == 7 else c["jour"]
    m = _midis(builder_from_plats(plats, previous=previous))
    # F (jours 6 et 7) reste un seul groupe ; les autres gardent le leur.
    assert [m[j]["group"] for j in range(1, 6)] == [1, 2, 3, 4, 5]
    assert m[6]["group"] == m[7]["group"] == 6


# ── Créneaux vides et convives ─────────────────────────────────────────


def test_creneau_vide_de_la_periode_garde_les_convives():
    plats = [_plat(1, "A", persons=3), _plat(3, "B", persons=5)]
    b = builder_from_plats(plats, off_meals=["2:soir"], default_persons=4, nb_jours=4)
    by = {(c["jour"], c["moment"]): c for c in b}
    assert by[(1, "soir")]["persons"] == 3          # convives du jour
    assert by[(2, "midi")]["persons"] == 4          # jour vide : convives du planning
    assert by[(2, "soir")]["persons"] == 0          # absent (off_meals)
    assert by[(4, "midi")]["persons"] == 4          # dans la période
    assert by[(5, "midi")]["persons"] == 0          # hors période


def test_creneau_vide_garde_les_convives_de_l_ancienne_grille():
    previous = builder_from_plats([_plat(1, "A")], nb_jours=2)
    for c in previous:
        if (c["jour"], c["moment"]) == (2, "soir"):
            c["persons"] = 6
    b = builder_from_plats([_plat(1, "A")], previous=previous, nb_jours=2)
    assert next(c for c in b if (c["jour"], c["moment"]) == (2, "soir"))["persons"] == 6


def test_grille_precedente_illisible_ignoree():
    plats = [_plat(1, "A"), _plat(2, "B")]
    previous = ["x", {"jour": "?", "moment": "midi"}, {"jour": 1, "moment": "midi", "group": "g"}]
    m = _midis(builder_from_plats(plats, previous=previous))
    assert (m[1]["group"], m[2]["group"]) == (1, 2)
