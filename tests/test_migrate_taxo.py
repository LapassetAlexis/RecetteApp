"""Tests de la logique PURE de migration de taxonomie (sans réseau).

On teste `derive_changes` : à partir de l'état BRUT d'une page Notion, elle
calcule les seules propriétés à changer (idempotente)."""

from scripts.migrate_taxo import derive_changes, _changes_to_properties


def _raw(**kw):
    base = {"nom": "R", "repas": [], "tags": [], "base": [], "nature": "", "moment": ""}
    base.update(kw)
    return base


def test_nature_defaut_recette_si_vide():
    changes = derive_changes(_raw())
    assert changes["nature"] == "Recette"


def test_tag_viande_vers_base():
    changes = derive_changes(_raw(tags=["Viande", "Rapide"]))
    assert changes["base"] == ["Viande"]
    assert changes["tags"] == ["Rapide"]  # Viande retiré des tags


def test_tag_poisson_vers_base():
    changes = derive_changes(_raw(tags=["Poisson"]))
    assert changes["base"] == ["Poisson"]
    assert changes["tags"] == []


def test_legumes_depuis_tag_et_repas():
    # tag « Légumes » OU repas « Légume »/« Accompagnement » -> Base « Légume ».
    c1 = derive_changes(_raw(tags=["Légumes"]))
    assert c1["base"] == ["Légume"] and c1["tags"] == []
    c2 = derive_changes(_raw(repas=["Plat", "Légume"]))
    assert c2["base"] == ["Légume"] and c2["repas"] == ["Plat"]
    c3 = derive_changes(_raw(repas=["Accompagnement"]))
    assert c3["base"] == ["Légume"] and c3["repas"] == []  # ne force pas « Plat »


def test_base_multi_valeurs():
    changes = derive_changes(_raw(tags=["Viande", "Légumes"], repas=["Plat"]))
    assert changes["base"] == ["Viande", "Légume"]


def test_moment_depuis_tags_midi_soir():
    # Moment vide + tags Midi & Soir -> « Les deux », tags retirés.
    c = derive_changes(_raw(tags=["Midi", "Soir"]))
    assert c["moment"] == "Les deux"
    assert c["tags"] == []
    # Un seul tag.
    assert derive_changes(_raw(tags=["Midi"]))["moment"] == "Midi"
    assert derive_changes(_raw(tags=["Soir"]))["moment"] == "Soir"


def test_moment_deja_rempli_ne_change_pas():
    # Moment déjà posé -> on RETIRE juste les tags, sans toucher au Moment.
    c = derive_changes(_raw(tags=["Midi"], moment="Soir"))
    assert "moment" not in c          # inchangé
    assert c["tags"] == []            # tag Midi retiré


def test_renommage_vegetarien():
    c = derive_changes(_raw(tags=["Végétarien proténiné", "Diet"]))
    assert c["tags"] == ["Végétarien", "Diet"]


def test_idempotence_recette_deja_migree():
    # Déjà migrée : nature posée, pas de signal hérité -> aucun changement.
    already = _raw(repas=["Plat"], tags=["Rapide"], base=["Viande"],
                   nature="Recette", moment="Midi")
    assert derive_changes(already) == {}


def test_idempotence_reapplication():
    # Appliquer derive_changes une 2e fois (sur l'état résultant) ne change rien.
    raw = _raw(tags=["Viande", "Midi", "Légumes"], repas=["Plat", "Accompagnement"])
    changes = derive_changes(raw)
    migrated = dict(raw)
    migrated.update(changes)
    assert derive_changes(migrated) == {}


def test_changes_to_properties_payload():
    props = _changes_to_properties({
        "nature": "Recette", "repas": ["Plat"], "tags": ["Rapide"],
        "base": ["Viande"], "moment": "Midi",
    })
    assert props["Nature"] == {"select": {"name": "Recette"}}
    assert props["Base"] == {"multi_select": [{"name": "Viande"}]}
    assert props["Repas"] == {"multi_select": [{"name": "Plat"}]}
    assert props["Tag"] == {"multi_select": [{"name": "Rapide"}]}
    assert props["Moment"] == {"select": {"name": "Midi"}}
