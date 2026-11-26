"""M05 — page planning (créneaux vides) et constructeur (libellés, réouverture
d'une semaine validée, couleurs des groupes, modales par Common.modal)."""

import json
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import main
from tests.test_brouillons_integrite import _catalogue, _data
from tests.test_routes import _case, _recipe

APP_DIR = Path(main.__file__).resolve().parent
STATIC = APP_DIR / "static"


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


POULET = _recipe("Poulet", id="p1")
CURRY = _recipe("Curry", id="c1")


def _trois_jours(client, monkeypatch, off=()) -> int:
    """Planning de 3 jours : midi 1 et soir 2 remplis, 4 créneaux vides
    (moins ceux passés à 0 convive dans `off`)."""
    _catalogue(monkeypatch, POULET, CURRY)
    cases = []
    for j in range(1, 8):
        for m in ("midi", "soir"):
            main_ = {(1, "midi"): POULET, (2, "soir"): CURRY}.get((j, m))
            pers = 0 if j > 3 or (j, m) in off else 2
            cases.append(_case(j, m, main_, persons=pers))
    r = client.post("/construire", data={
        "week_start": "2026-11-25", "meals": json.dumps(cases), "nb_jours": "3",
    }, follow_redirects=False)
    return int(r.headers["location"].rsplit("/", 1)[1])


def _section(html: str, cls: str) -> str:
    """Bloc `<div class="week-wrapper">` ou `<div class="week-list">`."""
    debut = html.index(f'<div class="{cls}">')
    fin = html.index("<!--", debut + 1) if cls == "week-wrapper" else html.index('id="bulk"', debut)
    return html[debut:fin]


# ── Créneaux vides ────────────────────────────────────────────────────────


def test_creneaux_vides_grille_et_liste(client, monkeypatch):
    pid = _trois_jours(client, monkeypatch)
    html = client.get(f"/planning/{pid}").text
    for bloc in (_section(html, "week-wrapper"), _section(html, "week-list")):
        vides = re.findall(r'class="meal-add" data-jour="(\d)" data-moment="(\w+)"', bloc)
        assert sorted(vides) == [("1", "soir"), ("2", "midi"), ("3", "midi"), ("3", "soir")]
    # Grille : chaque case vide est placée sur sa colonne / sa ligne.
    assert 'class="meal-empty soir" style="grid-column:2 / span 1;grid-row:3;"' in html


def test_creneau_absent_ni_hors_periode_pas_de_case_ajouter(client, monkeypatch):
    pid = _trois_jours(client, monkeypatch, off=[(3, "soir")])
    html = client.get(f"/planning/{pid}").text
    grille = _section(html, "week-wrapper")
    vides = re.findall(r'data-jour="(\d)" data-moment="(\w+)"', grille)
    assert ("3", "soir") not in vides            # absent : case « Absent »
    assert all(int(j) <= 3 for j, _ in vides)    # jours 4-7 hors période
    assert "🚫 Absent" in grille


def test_planning_plein_sans_case_ajouter(client, monkeypatch):
    _catalogue(monkeypatch, POULET)
    cases = [_case(j, m, POULET if j == 1 else None, persons=2 if j == 1 else 0)
             for j in range(1, 8) for m in ("midi", "soir")]
    r = client.post("/construire", data={"week_start": "2026-11-25", "meals": json.dumps(cases),
                                         "nb_jours": "1"}, follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    assert "meal-add" not in client.get(f"/planning/{pid}").text


def test_set_main_remplit_un_creneau_vide(client, monkeypatch):
    """Contrat utilisé par la case « Ajouter » : set_main crée le plat."""
    pid = _trois_jours(client, monkeypatch)
    r = client.post(f"/api/planning/{pid}/meals",
                    json={"slots": [{"jour": 3, "moment": "midi"}], "action": "set_main", "nom": "Curry"})
    assert r.status_code == 200 and r.json()["success"]
    plat = next(p for p in _data(pid)["plats"] if (p["jour"], p["moment"]) == (3, "midi"))
    assert plat["nom_recette"] == "Curry"
    html = client.get(f"/planning/{pid}").text
    assert 'data-jour="3" data-moment="midi"' not in html


def test_page_planning_sans_courses_checked(client, monkeypatch):
    pid = _trois_jours(client, monkeypatch)
    html = client.get(f"/planning/{pid}").text
    data = json.loads(re.search(r'id="page-data">(.*?)</script>', html, re.DOTALL).group(1))
    assert set(data) == {"pid", "day_labels"}


# ── Constructeur : libellés (D1) ──────────────────────────────────────────


def test_nouveau_planning_libelles(client):
    html = client.get("/").text
    assert "Valider ma semaine" not in html
    assert "📅 Enregistrer et voir le planning" in html
    assert "💾 Enregistrer le brouillon" in html
    assert "edit-banner" not in html and 'id="btn-cancel"' not in html


def test_brouillon_rouvert_libelles_de_brouillon(client, monkeypatch):
    pid = _trois_jours(client, monkeypatch)
    html = client.get(f"/?draft={pid}").text
    assert "📅 Enregistrer et voir le planning" in html and "edit-banner" not in html


def test_semaine_validee_rouverte_bandeau_et_libelles(client, monkeypatch):
    pid = _trois_jours(client, monkeypatch)
    client.post(f"/planning/{pid}/valider")
    html = client.get(f"/?planning={pid}").text
    assert "Tu modifies la semaine validée du" in html and "<strong>25/11/2026</strong>" in html
    assert "✅ Enregistrer les modifications" in html
    assert f'<a href="/planning/{pid}" class="btn btn-ghost btn-lg" id="btn-cancel">Annuler</a>' in html
    assert "Enregistrer le brouillon" not in html and "Valider ma semaine" not in html
    # Boutons gardés (mêmes id, mêmes actions) : « valider » mène au planning.
    assert 'id="btn-b" data-action="valider"' in html and 'id="btn-draft" data-action="brouillon"' in html


def test_semaine_validee_rouverte_enregistrer_reste_validee(client, monkeypatch):
    pid = _trois_jours(client, monkeypatch)
    client.post(f"/planning/{pid}/valider")
    cases = [_case(1, "midi", CURRY, persons=2)] + [
        _case(j, m, None, persons=2 if j <= 3 else 0)
        for j in range(1, 8) for m in ("midi", "soir") if (j, m) != (1, "midi")]
    r = client.post("/construire", data={"week_start": "2026-11-25", "meals": json.dumps(cases),
                                         "draft_id": str(pid), "nb_jours": "3", "action": "valider"},
                    follow_redirects=False)
    assert r.headers["location"] == f"/planning/{pid}"
    assert "Semaine validée" in client.get(f"/planning/{pid}").text


# ── Fichiers statiques ────────────────────────────────────────────────────


def _js(name: str) -> str:
    return (STATIC / "js" / name).read_text(encoding="utf-8")


def test_sept_couleurs_distinctes_pour_les_groupes():
    m = re.search(r"const GROUP_COLORS = \[([^\]]*)\]", _js("builder.js"))
    couleurs = [c.strip().strip("'").lower() for c in m.group(1).split(",")]
    assert len(couleurs) == 7 and len(set(couleurs)) == 7


def test_modales_par_common_modal():
    assert "modal($('modal')" in _js("planning-week.js")
    assert "modal($('pk')" in _js("builder-picker.js")
    assert "window.Common.modal(pop" in _js("builder.js")
    # Plus de gestion d'Échap à la main pour ces modales.
    assert "e.key === 'Escape' && $('modal')" not in _js("planning-week.js")


def test_contournement_spinner_retire():
    src = _js("builder.js")
    assert "nav-progress" not in src and "classList.remove('loading')" not in src
