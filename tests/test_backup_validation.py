"""M10 — import d'une sauvegarde : forme des repas vérifiée, jeton de partage
déjà pris retiré en fusion, ingrédients « sel, poivre » éclatés à la
réhydratation."""

import asyncio
import json

import pytest

from app.database import Database
from app.services import backup


def _db(path) -> Database:
    db = Database()
    db.path = str(path)
    asyncio.run(db.init())
    return db


def _planning(pid, data, week_start="2026-01-05", created_at="2026-01-01T10:00:00"):
    return {"id": pid, "week_start": week_start, "created_at": created_at,
            "data_json": json.dumps(data), "valide": 1, "recipes": []}


def _backup(*plannings):
    return {"version": 1, "plannings": list(plannings), "enriched": [], "prefs": {}}


PLAT = {"jour": 1, "moment": "midi", "nom_recette": "Poulet"}


# ── Forme des plats ──────────────────────────────────────────────────────


def test_plats_valides_ou_absents_acceptes():
    clean = backup.validate_backup(_backup(
        _planning(1, {"plats": [PLAT, {**PLAT, "jour": 7, "moment": "soir"}]}),
        _planning(2, {"liste_courses": []}),       # pas de plats : ancien format toléré
        _planning(3, {"plats": []}),
    ))
    assert [p["id"] for p in clean["plannings"]] == [1, 2, 3]


@pytest.mark.parametrize("plats, attendu", [
    ({"jour": 1}, "plats doit être une liste"),
    (["Poulet"], "plat n°1 invalide"),
    ([PLAT, {**PLAT, "jour": 0}], "plat n°2 invalide"),
    ([{**PLAT, "jour": 8}], "plat n°1 invalide"),
    ([{**PLAT, "jour": "1"}], "plat n°1 invalide"),
    ([{**PLAT, "jour": True}], "plat n°1 invalide"),
    ([{**PLAT, "moment": "matin"}], "plat n°1 invalide"),
    ([{"jour": 1, "moment": "midi"}], "plat n°1 invalide"),
    ([{**PLAT, "nom_recette": None}], "plat n°1 invalide"),
    (None, "plats doit être une liste"),
])
def test_plats_mal_formes_refuses(plats, attendu):
    with pytest.raises(backup.BackupError, match=attendu) as e:
        backup.validate_backup(_backup(_planning(4, {"plats": plats})))
    assert "planning n°1" in str(e.value)


def test_import_refuse_sans_rien_modifier(tmp_path):
    db = _db(tmp_path / "x.db")
    asyncio.run(backup.import_all(db, _backup(_planning(1, {"plats": [PLAT]})), "replace"))
    avant = asyncio.run(db.dump_all())
    mauvais = _backup(_planning(2, {"plats": [{**PLAT, "jour": 9}]}, week_start="2026-02-02"))
    for mode in ("merge", "replace"):
        with pytest.raises(backup.BackupError):
            asyncio.run(backup.import_all(db, mauvais, mode))
    assert asyncio.run(db.dump_all()) == avant


# ── Jeton de partage en double (fusion) ─────────────────────────────────


def _jetons(db) -> dict[str, str | None]:
    return {p["week_start"]: json.loads(p["data_json"]).get("share_token")
            for p in asyncio.run(db.dump_all())["plannings"]}


def test_fusion_retire_un_jeton_deja_pris_localement(tmp_path):
    db = _db(tmp_path / "x.db")
    asyncio.run(backup.import_all(db, _backup(
        _planning(1, {"plats": [PLAT], "share_token": "jeton-local"})), "replace"))
    asyncio.run(backup.import_all(db, _backup(
        _planning(7, {"plats": [PLAT], "share_token": "jeton-local"}, week_start="2026-02-02"),
        _planning(8, {"plats": [PLAT], "share_token": "jeton-neuf"}, week_start="2026-03-02"),
    ), "merge"))
    assert _jetons(db) == {"2026-01-05": "jeton-local", "2026-02-02": None, "2026-03-02": "jeton-neuf"}
    # Le lien public désigne toujours le planning local, et lui seul.
    assert asyncio.run(db.get_planning_by_share_token("jeton-local"))["week_start"] == "2026-01-05"
    assert asyncio.run(db.get_planning_by_share_token("jeton-neuf"))["week_start"] == "2026-03-02"


def test_fusion_retire_un_jeton_en_double_dans_le_fichier(tmp_path):
    db = _db(tmp_path / "x.db")
    asyncio.run(backup.import_all(db, _backup(
        _planning(1, {"plats": [PLAT], "share_token": "double"}),
        _planning(2, {"plats": [PLAT], "share_token": "double"}, week_start="2026-02-02"),
    ), "merge"))
    assert _jetons(db) == {"2026-01-05": "double", "2026-02-02": None}


def test_fusion_planning_deja_present_garde_son_jeton(tmp_path):
    db = _db(tmp_path / "x.db")
    sauvegarde = _backup(_planning(1, {"plats": [PLAT], "share_token": "j1"}))
    asyncio.run(backup.import_all(db, sauvegarde, "replace"))
    # Ré-importer la même sauvegarde : planning déjà présent, ignoré, jeton intact.
    added = asyncio.run(backup.import_all(db, sauvegarde, "merge"))
    assert added["plannings"] == 0
    assert _jetons(db) == {"2026-01-05": "j1"}


def test_remplacement_garde_les_jetons(tmp_path):
    db = _db(tmp_path / "x.db")
    asyncio.run(backup.import_all(db, _backup(
        _planning(1, {"plats": [PLAT], "share_token": "j1"})), "replace"))
    asyncio.run(backup.import_all(db, _backup(
        _planning(1, {"plats": [PLAT], "share_token": "j1"})), "replace"))
    assert _jetons(db) == {"2026-01-05": "j1"}


# ── Réhydratation : « sel, poivre » donne deux ingrédients ──────────────


def test_ingredients_from_text_eclate_les_condiments():
    assert backup.ingredients_from_text("- 200 g de riz\n- sel, poivre") == [
        {"nom": "riz", "quantite": "200", "unite": "g"},
        {"nom": "sel", "quantite": "", "unite": ""},
        {"nom": "poivre", "quantite": "", "unite": ""},
    ]
    # Une virgule qui n'est pas une liste de condiments reste entière.
    assert [i["nom"] for i in backup.ingredients_from_text("- farine, tamisée")] == ["farine, tamisée"]
