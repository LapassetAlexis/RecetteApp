"""Cohérence de la bibliothèque de briques de départ."""

from app.config import BASE_OPTIONS
from scripts.seed_briques import _BRIQUES


def test_briques_bases_valides_et_noms_uniques():
    noms = [b[0] for b in _BRIQUES]
    assert len(noms) == len(set(noms))                       # pas de doublon
    assert all(base in BASE_OPTIONS for _, base, _, _ in _BRIQUES)  # Base connue
    assert all(nom.strip() and qte.strip() for nom, _, qte, _ in _BRIQUES)
    # couvre au moins protéine, féculent et légume (nécessaires aux repas simples)
    bases = {b[1] for b in _BRIQUES}
    assert {"Viande", "Poisson", "Féculent", "Légume"} <= bases
