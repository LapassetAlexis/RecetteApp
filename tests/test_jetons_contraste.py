"""M07 — contraste des jetons de couleur (D5, N14).

Les ratios sont calculés (formule WCAG 2.x) depuis le bloc `:root` de
`style.css` : aucun couple texte / fond des jetons sous 4,5:1. L'orange et le
vert vifs restent des couleurs de fond ; le texte posé dessus est foncé."""

import re
from pathlib import Path

import pytest

STYLE = Path(__file__).resolve().parents[1] / "app" / "static" / "style.css"
SEUIL = 4.5


def _root() -> dict[str, str]:
    css = STYLE.read_text(encoding="utf-8")
    bloc = re.search(r":root\s*\{(.*?)\}", css, re.DOTALL).group(1)
    bloc = re.sub(r"/\*.*?\*/", "", bloc, flags=re.DOTALL)
    return {k: v.strip() for k, v in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", bloc)}


def _couleur(val: str, jetons: dict[str, str]) -> str:
    """Résout `var(--x)` (en chaîne) jusqu'à une couleur hexadécimale."""
    for _ in range(10):
        m = re.fullmatch(r"var\(\s*(--[\w-]+)\s*\)", val)
        if not m:
            break
        val = jetons[m.group(1)]
    assert re.fullmatch(r"#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}", val), val
    return val


def _luminance(hexa: str) -> float:
    h = hexa.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    canaux = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in canaux]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contraste(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_formule_de_reference():
    assert contraste("#000", "#fff") == pytest.approx(21)
    assert contraste("#fff", "#fff") == pytest.approx(1)
    # constat N14 : orange et vert vifs sous texte blanc
    assert contraste("#e67e22", "#fff") == pytest.approx(2.85, abs=0.01)
    assert contraste("#27ae60", "#fff") == pytest.approx(2.87, abs=0.01)


# (texte, fond) : jetons ou couleurs littérales.
COUPLES = [
    # texte courant et secondaire
    ("--text", "--surface"), ("--text", "--bg"),
    ("--t2", "--surface"), ("--t2", "--bg"),
    # texte orange (titres de rayon, puces, liens) et vert (succès)
    ("--pr-text", "--surface"), ("--pr-text", "--bg"),
    ("--prd", "--surface"), ("--prd", "--bg"),
    ("--pri", "--surface"), ("--pri", "--bg"),
    ("--sc-text", "--surface"), ("--sc-text", "--bg"),
    # texte posé sur les fonds orange / vert (boutons, puces actives, toasts)
    ("--on-pr", "--pr"), ("--on-pr", "--prl"), ("--on-sc", "--sc"),
    # fonds encore sous texte blanc (css/*.css, en-têtes du soir)
    ("#fff", "--pri"), ("#fff", "--prd"), ("#fff", "--ac"),
]


@pytest.mark.parametrize(("texte", "fond"), COUPLES)
def test_couples_des_jetons_au_moins_4_5(texte, fond):
    jetons = _root()
    t = _couleur(f"var({texte})" if texte.startswith("--") else texte, jetons)
    f = _couleur(f"var({fond})" if fond.startswith("--") else fond, jetons)
    assert contraste(t, f) >= SEUIL, f"{texte} sur {fond} : {contraste(t, f):.2f}:1"


def test_orange_vif_garde_pour_les_fonds():
    jetons = _root()
    assert _couleur("var(--pr)", jetons).lower() == "#e67e22"
    assert _couleur("var(--sc)", jetons).lower() == "#27ae60"


def _regles() -> list[tuple[str, str]]:
    css = re.sub(r"/\*.*?\*/", "", STYLE.read_text(encoding="utf-8"), flags=re.DOTALL)
    return re.findall(r"([^{}]+)\{([^{}]*)\}", css)


def test_aucun_texte_en_orange_ou_vert_vif():
    fautes = [sel.strip() for sel, corps in _regles()
              if re.search(r"(?<![-\w])color\s*:\s*var\(--(pr|sc)\)", corps)]
    assert not fautes, fautes


def test_texte_fonce_sur_fond_orange_ou_vert():
    """Toute règle qui pose un fond orange / vert vif fixe un texte --on-*."""
    fautes = []
    for sel, corps in _regles():
        if re.search(r"background(-color)?\s*:\s*[^;]*var\(--(pr|sc|prl)\)", corps) \
                and re.search(r"(?<![-\w])color\s*:", corps):
            if not re.search(r"(?<![-\w])color\s*:\s*var\(--on-(pr|sc)\)", corps):
                fautes.append(sel.strip())
    assert not fautes, fautes


def test_une_seule_regle_de_bouton_secondaire():
    regles = [sel for sel, _ in _regles() if re.search(r"\.btn-secondary\b(?![-\w])", sel)]
    assert any(".btn-ghost" in sel for sel in regles)
    for sel in regles:  # pas de règle propre au vert
        assert ".btn-ghost" in sel or ".loading" in sel, sel


# ── Pastilles de groupe du constructeur (texte blanc sur la couleur du groupe) ──

STATIC = STYLE.parent


def _palette_groupes() -> list[str]:
    js = (STATIC / "js" / "builder.js").read_text(encoding="utf-8")
    bloc = re.search(r"const GROUP_COLORS = \[([^\]]*)\]", js).group(1)
    return re.findall(r"'(#[0-9a-fA-F]{6})'", bloc)


def test_pastille_de_groupe_en_texte_blanc():
    css = (STATIC / "css" / "builder.css").read_text(encoding="utf-8")
    corps = re.search(r"\.b-group\s*\{([^}]*)\}", css).group(1)
    assert re.search(r"(?<![-\w])color\s*:\s*#fff\b", corps)


def test_palette_des_groupes_complete_et_distincte():
    palette = _palette_groupes()
    assert len(palette) == 7 and len({c.lower() for c in palette}) == 7


@pytest.mark.parametrize("couleur", _palette_groupes())
def test_palette_des_groupes_au_moins_4_5_sous_texte_blanc(couleur):
    assert contraste("#fff", couleur) >= SEUIL, f"G sur {couleur} : {contraste('#fff', couleur):.2f}:1"
