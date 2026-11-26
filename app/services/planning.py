"""Plats d'un planning : accompagnements, infos des recettes, repas identiques
regroupés, libellés des jours et de la semaine, période (jours 1..nb_jours), aperçu.

Le reste du planning vit dans des modules dédiés :
- `planning_builder` : grille du constructeur reconstruite depuis les plats ;
- `planning_construire` : planning construit depuis le constructeur, copie ;
- `planning_batch` : édition groupée des repas, briques, recettes proposées ;
- `shopping` : rappel des repas libres dans la liste de courses.
Les anciens noms restent importables d'ici (voir `compat`)."""

import json
from datetime import date, timedelta

from app.config import recipe_base, recipe_nature, recipe_types
from app.services.compat import anciens_noms
from app.text_utils import clean_recipe_title


def index_by_name(recettes: list[dict]) -> dict[str, dict]:
    """Index {nom normalisé: recette} pour un lookup O(1) (évite les scans
    linéaires répétés sur toute la base à chaque appariement de plat)."""
    return {r["nom"].lower().strip(): r for r in recettes}


def plat_accompagnements(plat: dict) -> list[dict]:
    """Lecture TOLÉRANTE des accompagnements d'un plat.

    Nouveau modèle : liste `accompagnements` (0..N). Ancien modèle (anciens
    plannings) : `accompagnement` (dict | None) → traité comme liste à 0/1.
    Filtre les entrées vides (sans nom_recette)."""
    accs = plat.get("accompagnements")
    if accs is not None:
        return [a for a in accs if a and a.get("nom_recette")]
    acc = plat.get("accompagnement")
    return [acc] if acc and acc.get("nom_recette") else []


def _apply_recipe_info(plat: dict, r: dict) -> None:
    """Reporte les infos Notion d'une recette sur un plat du planning."""
    plat["notion_id"] = r["id"]
    plat["url"] = r.get("url", "")
    plat["notion_url"] = r.get("notion_url", "")
    plat["repas"] = r.get("repas", [])
    plat["base"] = r.get("base", [])
    plat["nature"] = recipe_nature(r)
    plat["tags"] = r.get("tags", [])


def group_week(plats: list[dict]) -> dict[str, list[dict]]:
    """Regroupe les jours consécutifs partageant le même repas (plat + même
    accompagnement) en « runs » pour fusionner les cases du planning.
    Retourne {"midi": [run, ...], "soir": [...]} avec run = {start, span, jours,
    plat, parts} — `parts` = total des convives des jours fusionnés, c'est-à-dire
    le nombre de portions que ce repas pèse dans la liste de courses."""
    def _pers(plat: dict) -> int:
        try:
            return max(0, int(plat.get("persons") or 0))
        except (TypeError, ValueError):
            return 0

    rows: dict[str, list[dict]] = {"midi": [], "soir": []}
    for moment in ("midi", "soir"):
        by_day = {p["jour"]: p for p in plats if p["moment"] == moment}
        run: dict | None = None
        for jour in range(1, 8):
            plat = by_day.get(jour)
            if not plat:
                run = None
                continue
            accs = plat_accompagnements(plat)
            key = (
                plat.get("nom_recette", ""),
                tuple(a.get("nom_recette", "") for a in accs),
            )
            if run and run["key"] == key and run["start"] + run["span"] == jour:
                run["span"] += 1
                run["jours"].append(jour)
                run["parts"] += _pers(plat)
            else:
                run = {"key": key, "start": jour, "span": 1, "jours": [jour],
                       "plat": plat, "parts": _pers(plat)}
                rows[moment].append(run)
    return rows


def per_day_list(planning: dict, data: dict) -> list[int]:
    """Personnes par jour du planning (repli sur nb_personnes pour tous les
    anciens plannings sans `per_day`)."""
    raw = data.get("per_day", "")
    try:
        per_day = [int(x) for x in raw.split(",")] if raw else []
    except (ValueError, AttributeError):
        per_day = []
    if len(per_day) != 7:
        per_day = [planning.get("nb_personnes", 4)] * 7
    return per_day


# ── Libellés des jours et de la semaine ─────────────────────────────────

_JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def _jour1(week_start: str) -> date | None:
    """Date du jour 1 (`week_start`, AAAA-MM-JJ), ou None si invalide."""
    try:
        y, m, d = (int(x) for x in week_start.split("-"))
        return date(y, m, d)
    except (ValueError, AttributeError):
        return None


def day_labels(week_start: str) -> list[dict]:
    """Libellés des jours 1..7 du planning, DÉRIVÉS de la date du jour 1
    (`week_start`). Le jour 1 = `week_start`, le jour 2 = +1j, … le jour 7 = +6j.

    Chaque entrée : {jour (1..7), court (3 lettres capitalisées, ex. « Lun »),
    long (nom complet capitalisé, ex. « Lundi »), date « JJ/MM »}. Le nom du jour
    vient du VRAI jour de semaine (`date.weekday()`), pas de l'index. Repli neutre
    (« J1 » / « Jour 1 » / date vide) si `week_start` est invalide."""
    start = _jour1(week_start)
    out: list[dict] = []
    for j in range(1, 8):
        if start:
            dt = start + timedelta(days=j - 1)
            nom = _JOURS_FR[dt.weekday()]
            out.append({
                "jour": j,
                "court": nom[:3].capitalize(),
                "long": nom.capitalize(),
                "date": f"{dt.day:02d}/{dt.month:02d}",
            })
        else:
            out.append({"jour": j, "court": f"J{j}", "long": f"Jour {j}", "date": ""})
    return out


def week_label(week_start: str, plats: list, nb_jours=None) -> str:
    """Titre reflétant la PLAGE de jours du planning (ex. « Mardi 14/07 →
    vendredi 17/07 ») : du premier jour porteur d'un plat jusqu'à la fin de la
    période (`nb_jours`, comme la grille), ou jusqu'au dernier jour porteur
    d'un plat pour les anciens plannings sans période. Repli sur « Semaine du
    <date> » si rien d'exploitable."""
    jours = sorted({p.get("jour") for p in plats
                    if isinstance(p.get("jour"), int) and 1 <= p["jour"] <= 7})
    start = _jour1(week_start)
    if not jours or start is None:
        return f"Semaine du {week_start}"
    fin = nb_jours_periode({"plats": plats, "nb_jours": nb_jours}) if nb_jours else jours[-1]

    def _fmt(j: int) -> str:
        # Le nom du jour vient du VRAI jour de semaine (week_start + j-1),
        # pas de l'index j (week_start = date du jour 1, un jour quelconque).
        dt = start + timedelta(days=j - 1)
        return f"{_JOURS_FR[dt.weekday()]} {dt.day:02d}/{dt.month:02d}"

    if jours[0] == fin:
        return _fmt(jours[0]).capitalize()
    return f"{_fmt(jours[0]).capitalize()} → {_fmt(fin)}"


def attach_label(p: dict) -> dict:
    """Ajoute `label` (plage de jours de la période) à une ligne de planning listée."""
    try:
        data = json.loads(p.get("data_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    p["label"] = week_label(p.get("week_start", ""), data.get("plats", []), data.get("nb_jours"))
    return p


# ── Plats et accompagnements ────────────────────────────────────────────


def build_plat(item: dict, jour: int, moment: str, persons: int,
               by_id: dict[str, dict]) -> dict:
    """Construit un plat du planning à partir d'un item choisi dans le catalogue.
    Reporte les infos Notion (base, nature, type, url…) si la recette existe."""
    plat: dict = {
        "jour": jour,
        "moment": moment,
        "persons": persons,
        "nom_recette": item["nom"],
        "type_repas": "Plat",
        "notion_id": "",
        "url": "",
        "notion_url": "",
        "nature": item.get("nature") or "Recette",
        "base": [],
        "repas": [],
        "tags": [],
        "accompagnements": [],
        # Repas libre : voir parse_builder_meals. Reporté pour l'affichage et le
        # rappel « ingrédients à acheter » dans la liste de courses.
        "libre": bool(item.get("libre")),
    }
    r = by_id.get(item.get("notion_id", ""))
    if r:
        _apply_recipe_info(plat, r)
    return plat


def build_side(item: dict, by_id: dict[str, dict]) -> dict:
    """Construit un accompagnement (dict standard) depuis un item du catalogue."""
    side: dict = {
        "nom_recette": item["nom"],
        "notion_id": item.get("notion_id", "") or "",
        "url": "",
        "notion_url": "",
        # Accompagnement libre : titre saisi à la main, sans recette Notion →
        # aucun ingrédient en cache, rappel dans la liste de courses.
        "libre": bool(item.get("libre")) or not item.get("notion_id"),
    }
    r = by_id.get(item.get("notion_id", ""))
    if r:
        side.update(
            libre=False,
            notion_id=r["id"],
            url=r.get("url", ""),
            notion_url=r.get("notion_url", ""),
            nature=recipe_nature(r),
            base=recipe_base(r),
        )
    return side


def _set_main(target: dict, nom: str, recette: dict | None) -> None:
    """Remplace le plat principal d'un repas : nouveau nom, infos Notion de
    `recette` si elle existe, et assiette vide (les accompagnements de l'ancien
    plat ne le concernent plus ; l'utilisateur les recompose ensuite)."""
    target["nom_recette"] = nom
    target["notion_id"] = target["url"] = target["notion_url"] = ""
    if recette:
        _apply_recipe_info(target, recette)
    target["accompagnements"] = []
    target.pop("accompagnement", None)


def set_main(target: dict, nom: str, recette: dict | None) -> None:
    """Nom public de `_set_main`. L'appel passe par le nom du module pour qu'un
    test puisse substituer `_set_main` (test_planning_batch)."""
    _set_main(target, nom, recette)


def recipe_rows(plats: list[dict]) -> list[dict]:
    """Lignes `planning_recipes` (une par plat) à enregistrer avec le planning."""
    return [
        {
            "notion_id": p.get("notion_id", ""),
            "recipe_name": p["nom_recette"],
            "repas_type": p.get("type_repas", ""),
            "jour": p["jour"],
            "moment": p["moment"],
        }
        for p in plats
    ]


def mark_non_enrichis(plats: list[dict], enriched_ids: set[str]) -> None:
    """Marque les repas (et accompagnements) dont la recette n'a pas
    d'ingrédients en cache (« recette pas terminée ») pour l'avertissement du
    planning, et normalise les anciens plannings pour l'affichage."""
    for p in plats:
        # Normalise le type en liste (anciens plannings : « repas » stocké en
        # string avant le passage au multi-valeurs) pour l'affichage en chips.
        p["repas"] = recipe_types(p)
        nid = p.get("notion_id")
        p["non_enrichi"] = not (nid and nid in enriched_ids)
        # Normalise vers le modèle liste (tolère les anciens plannings en
        # `accompagnement` singulier) et marque chaque accompagnement non enrichi.
        accs = plat_accompagnements(p)
        for acc in accs:
            aid = acc.get("notion_id")
            acc["non_enrichi"] = not (aid and aid in enriched_ids)
        p["accompagnements"] = accs


# ── Période du planning (jours 1..nb_jours) ──────────────────────────────


def _jours_plats(plats: list[dict]) -> list[int]:
    """Jours (1..7) portant un plat."""
    out = []
    for p in plats:
        try:
            j = int(p.get("jour"))
        except (TypeError, ValueError):
            continue
        if 1 <= j <= 7:
            out.append(j)
    return out


def nb_jours_periode(data: dict) -> int:
    """Dernier jour de la période du planning (1..7).

    `data["nb_jours"]` est enregistré par /construire. Repli pour les anciens
    plannings : dernier jour porteur d'un plat, sinon 7. On ne descend jamais
    sous le dernier jour porteur d'un plat (un repas ajouté après coup reste
    visible)."""
    dernier_plat = max(_jours_plats(data.get("plats") or []), default=0)
    try:
        n = int(data.get("nb_jours") or 0)
    except (TypeError, ValueError):
        n = 0
    if 1 <= n <= 7:
        return max(n, dernier_plat)
    return dernier_plat or 7


def parse_week_start(raw) -> str | None:
    """Date du jour 1 saisie (AAAA-MM-JJ), normalisée, ou None si invalide."""
    try:
        return date.fromisoformat(str(raw or "").strip()).isoformat()
    except ValueError:
        return None


# ── Aperçu d'un planning ─────────────────────────────────────────────────


def _meal_label(plat: dict) -> str:
    """Nom court d'un repas : plat + accompagnements (« Poulet + Riz »)."""
    nom = clean_recipe_title(plat.get("nom_recette", ""))
    accs = plat_accompagnements(plat)
    if accs:
        nom += " + " + " + ".join(clean_recipe_title(a.get("nom_recette", "")) for a in accs)
    return nom


def apercu_jours(plats: list[dict], week_start: str) -> list[dict]:
    """Aperçu compact d'une semaine : par jour portant un repas, son libellé
    court (dérivé de la date du jour 1) et le nom des repas midi et soir."""
    labels = day_labels(week_start)
    by = {(p.get("jour"), p.get("moment")): p for p in plats}
    jours = []
    for d in range(1, 8):
        midi, soir = by.get((d, "midi")), by.get((d, "soir"))
        if midi or soir:
            jours.append({
                "jour": labels[d - 1]["court"],
                "midi": _meal_label(midi) if midi else "",
                "soir": _meal_label(soir) if soir else "",
            })
    return jours


__getattr__ = anciens_noms(__name__)
