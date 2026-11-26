"""Règles métier du planning : grille du constructeur, plats, accompagnements,
libellés des jours, regroupement des repas identiques."""

import json
from datetime import date, timedelta

from app.config import recipe_base, recipe_nature, recipe_types
from app.deps import db, notion
from app.text_utils import clean_recipe_title, parse_ingredient_line


def _index_by_name(recettes: list[dict]) -> dict[str, dict]:
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


def _group_week(plats: list[dict]) -> dict[str, list[dict]]:
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


def _per_day_list(planning: dict, data: dict) -> list[int]:
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


_JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def _day_labels(week_start: str) -> list[dict]:
    """Libellés des jours 1..7 du planning, DÉRIVÉS de la date du jour 1
    (`week_start`). Le jour 1 = `week_start`, le jour 2 = +1j, … le jour 7 = +6j.

    Chaque entrée : {jour (1..7), court (3 lettres capitalisées, ex. « Lun »),
    long (nom complet capitalisé, ex. « Lundi »), date « JJ/MM »}. Le nom du jour
    vient du VRAI jour de semaine (`date.weekday()`), pas de l'index. Repli neutre
    (« J1 » / « Jour 1 » / date vide) si `week_start` est invalide."""
    try:
        y, m, d = (int(x) for x in week_start.split("-"))
        start = date(y, m, d)
    except (ValueError, AttributeError):
        start = None
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


def _week_label(week_start: str, plats: list) -> str:
    """Titre reflétant la PLAGE de jours réellement remplis (ex. « Mardi 14/07 →
    vendredi 17/07 »). Repli sur « Semaine du <date> » si rien d'exploitable."""
    jours = sorted({p.get("jour") for p in plats
                    if isinstance(p.get("jour"), int) and 1 <= p["jour"] <= 7})
    if not jours:
        return f"Semaine du {week_start}"
    try:
        y, m, d = (int(x) for x in week_start.split("-"))
        start = date(y, m, d)
    except (ValueError, AttributeError):
        return f"Semaine du {week_start}"

    def _fmt(j: int) -> str:
        # Le nom du jour vient du VRAI jour de semaine (week_start + j-1),
        # pas de l'index j (week_start = date du jour 1, un jour quelconque).
        dt = start + timedelta(days=j - 1)
        return f"{_JOURS_FR[dt.weekday()]} {dt.day:02d}/{dt.month:02d}"

    if jours[0] == jours[-1]:
        return _fmt(jours[0]).capitalize()
    return f"{_fmt(jours[0]).capitalize()} → {_fmt(jours[-1])}"


def _attach_label(p: dict) -> dict:
    """Ajoute `label` (plage de jours) à une ligne de planning listée."""
    try:
        plats = json.loads(p.get("data_json") or "{}").get("plats", [])
    except (json.JSONDecodeError, TypeError):
        plats = []
    p["label"] = _week_label(p.get("week_start", ""), plats)
    return p


def _json_or_none(raw: str):
    """Parse une chaîne JSON en objet Python, ou None si vide/illisible.
    Sert à re-stocker la grille brute du constructeur (`builder`)."""
    if not raw or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _parse_builder_meals(raw: str) -> list[dict]:
    """Parse le champ `meals` du constructeur en cases normalisées.

    Chaque case : {jour 1-7, moment midi|soir, persons >= 0 (0 = absent),
    group (int, midis uniquement), main {notion_id, nom, nature} | None,
    accompagnements [{notion_id, nom}, ...]}. Les entrées invalides sont
    ignorées ; retourne [] si le champ est vide/illisible."""
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []

    def _item(v) -> dict | None:
        if not isinstance(v, dict):
            return None
        nom = str(v.get("nom", "")).strip()
        if not nom:
            return None
        nid = str(v.get("notion_id", "") or "")
        return {
            "notion_id": nid,
            "nom": nom,
            "nature": v.get("nature") or "Recette",
            # Repas libre : titre saisi à la main, non lié à une recette Notion
            # (ex. « Pizzas »). Pas d'ingrédients en base → rappel dans les courses.
            "libre": bool(v.get("libre")) or not nid,
        }

    out: list[dict] = []
    for c in data:
        if not isinstance(c, dict):
            continue
        try:
            jour = int(c.get("jour"))
        except (TypeError, ValueError):
            continue
        moment = c.get("moment")
        if not (1 <= jour <= 7) or moment not in ("midi", "soir"):
            continue
        try:
            persons = max(0, int(c.get("persons", 0)))
        except (TypeError, ValueError):
            persons = 0
        accs: list[dict] = []
        raw_accs = c.get("accompagnements") or []
        if isinstance(raw_accs, list):
            for a in raw_accs:
                it = _item(a)
                if it:
                    accs.append(it)
        case: dict = {"jour": jour, "moment": moment, "persons": persons,
                      "main": _item(c.get("main")), "accompagnements": accs}
        if moment == "midi":
            try:
                case["group"] = int(c.get("group", jour))
            except (TypeError, ValueError):
                case["group"] = jour
        else:
            case["group"] = None
        out.append(case)
    return out


def _builder_item(src: dict) -> dict:
    """Item du constructeur {notion_id, nom, nature, libre} depuis un plat ou
    un accompagnement du planning."""
    nid = str(src.get("notion_id", "") or "")
    return {
        "notion_id": nid,
        "nom": src.get("nom_recette", ""),
        "nature": src.get("nature") or "Recette",
        "libre": bool(src.get("libre")) or not nid,
    }


def _builder_from_plats(plats: list[dict], off_meals: list[str] | None = None,
                        default_persons: int = 4) -> list[dict]:
    """Reconstruit la grille du constructeur (`builder`) depuis les plats.

    Sert aux anciens plannings sans `builder` et après une modification depuis
    la page planning (le constructeur rechargerait sinon l'ancienne grille).
    Une case par (jour, moment) sur 7 jours :
    - case avec plat : `persons` du plat (repli `default_persons`), plat
      principal + accompagnements ;
    - case sans plat : absente (persons 0) si elle est dans `off_meals` ou si
      le jour n'a aucun plat ; sinon vide avec les convives d'un plat du jour ;
    - midis : `group` = jour, sauf midis CONSÉCUTIFS au même repas (même
      plat, mêmes accompagnements) qui partagent le groupe du premier. Comme
      dans le constructeur, seul le premier midi du groupe porte le repas."""
    off = set(off_meals or [])
    by_slot: dict[tuple[int, str], dict] = {}
    for p in plats:
        try:
            key = (int(p.get("jour")), p.get("moment"))
        except (TypeError, ValueError):
            continue
        if 1 <= key[0] <= 7 and key[1] in ("midi", "soir"):
            by_slot.setdefault(key, p)

    def _persons(p: dict) -> int:
        try:
            n = int(p.get("persons") or 0)
        except (TypeError, ValueError):
            n = 0
        return n if n > 0 else default_persons

    def _signature(p: dict | None):
        if not p:
            return None
        return (
            str(p.get("notion_id", "") or ""), p.get("nom_recette", ""),
            tuple((str(a.get("notion_id", "") or ""), a.get("nom_recette", ""))
                  for a in plat_accompagnements(p)),
        )

    out: list[dict] = []
    prev_sig, prev_group = None, None
    for jour in range(1, 8):
        day_plats = [by_slot.get((jour, m)) for m in ("midi", "soir")]
        day_persons = next((_persons(p) for p in day_plats if p), 0)
        for moment in ("midi", "soir"):
            p = by_slot.get((jour, moment))
            case: dict = {"jour": jour, "moment": moment}
            if p:
                case["persons"] = _persons(p)
            elif f"{jour}:{moment}" in off:
                case["persons"] = 0
            else:
                case["persons"] = day_persons
            main_item = _builder_item(p) if p else None
            accs = [_builder_item(a) for a in plat_accompagnements(p)] if p else []
            if moment == "midi":
                sig = _signature(p)
                if sig is not None and sig == prev_sig:
                    case["group"] = prev_group
                    main_item, accs = None, []   # repas porté par le 1er midi
                else:
                    case["group"] = jour
                    prev_group = jour
                prev_sig = sig
            if main_item:
                case["main"] = main_item
            case["accompagnements"] = accs
            out.append(case)
    return out


def _build_plat(item: dict, jour: int, moment: str, persons: int,
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
        # Repas libre : voir _parse_builder_meals. Reporté pour l'affichage et le
        # rappel « ingrédients à acheter » dans la liste de courses.
        "libre": bool(item.get("libre")),
    }
    r = by_id.get(item.get("notion_id", ""))
    if r:
        _apply_recipe_info(plat, r)
    return plat


def _repas_libres(plats: list[dict], day_labels: list[dict] | None = None) -> list[dict]:
    """Repas ET accompagnements libres du planning (titre saisi à la main, sans
    recette liée).

    Renvoie [{nom, jour, moment, quand}] : sert de rappel « pense à acheter les
    ingrédients » dans la liste de courses (ces repas ne contribuent aucun
    ingrédient au cache, cf. _collect_shopping). `quand` = libellé humain
    (ex. « samedi soir ») dérivé de day_labels si fourni."""
    out: list[dict] = []
    for p in plats:
        jour, moment = p.get("jour"), p.get("moment")
        jour_lbl = ""
        try:
            if day_labels and 1 <= int(jour) <= len(day_labels):
                jour_lbl = day_labels[int(jour) - 1].get("long", "")
        except (TypeError, ValueError):
            pass
        quand = " ".join(x for x in (jour_lbl.lower(), moment) if x).strip()
        # Le plat, puis ses accompagnements libres (mêmes jour/moment).
        libres = [p] if p.get("libre") else []
        libres += [a for a in plat_accompagnements(p)
                   if a.get("libre") or not a.get("notion_id")]
        for src in libres:
            out.append({
                "nom": clean_recipe_title(src.get("nom_recette", "")),
                "jour": jour,
                "moment": moment,
                "quand": quand,
            })
    return out


def _build_side(item: dict, by_id: dict[str, dict]) -> dict:
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


async def _resolve_side(nom: str) -> dict:
    """Construit le dict accompagnement pour un nom de recette, en reliant à
    Notion si la recette y existe (sinon accompagnement « libre » sans id)."""
    acc = {"nom_recette": nom, "notion_id": "", "url": "", "notion_url": "",
           # Nom inconnu au catalogue → accompagnement LIBRE (rappel courses).
           "libre": True}
    r = _index_by_name(await notion.get_all_recipes()).get(nom.lower().strip())
    if r:
        acc.update(libre=False, notion_id=r["id"], url=r.get("url", ""),
                   notion_url=r.get("notion_url", ""))
    return acc


def _recipe_rows(plats: list[dict]) -> list[dict]:
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


async def _load_slot(planning_id: int, jour, moment):
    """Charge un planning et repère le repas (jour, moment) à modifier.

    Renvoie (planning, planning_data, plats, cible), ou {"error": …} si le
    planning ou le repas n'existe pas."""
    planning = await db.get_planning_with_recipes(planning_id)
    if not planning:
        return {"error": "Planning introuvable"}
    planning_data = json.loads(planning["data_json"])
    plats = planning_data["plats"]
    cible = next((p for p in plats if p["jour"] == jour and p["moment"] == moment), None)
    if not cible:
        return {"error": "Repas non trouvé dans le planning"}
    return planning, planning_data, plats, cible


def _plats_from_cases(actives: list[dict], by_id: dict[str, dict]) -> list[dict]:
    """Plats du planning depuis les cases actives du constructeur.

    Les midis d'un même groupe partagent le repas du 1er midi rempli du groupe
    (même couleur = même plat) ; une case sans plat principal est ignorée."""
    # Repas de référence des midis groupés : le 1er midi rempli du groupe.
    midi_group_meal: dict[int, dict] = {}
    for c in sorted((c for c in actives if c["moment"] == "midi"),
                    key=lambda c: c["jour"]):
        if c["main"] and c["group"] not in midi_group_meal:
            midi_group_meal[c["group"]] = c

    plats: list[dict] = []
    for c in actives:
        if c["moment"] == "midi":
            src = midi_group_meal.get(c["group"])
            if not src:
                continue  # aucun midi rempli dans ce groupe
            main_item, accs = src["main"], src["accompagnements"]
        else:
            if not c["main"]:
                continue
            main_item, accs = c["main"], c["accompagnements"]
        plat = _build_plat(main_item, c["jour"], c["moment"], c["persons"], by_id)
        plat["accompagnements"] = [_build_side(a, by_id) for a in accs]
        plats.append(plat)
    return plats


def _per_day_from_cases(cases: list[dict], nb_personnes: int) -> list[int]:
    """Personnes par jour (1..7) : le plus grand nombre de convives du midi et
    du soir, repli sur `nb_personnes` pour un jour vide."""
    by_key = {(c["jour"], c["moment"]): c for c in cases}
    per_day_list = []
    for d in range(1, 8):
        pm = by_key.get((d, "midi"), {}).get("persons", 0)
        ps = by_key.get((d, "soir"), {}).get("persons", 0)
        per_day_list.append(max(pm, ps) or nb_personnes)
    return per_day_list


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


def _mark_non_enrichis(plats: list[dict], enriched_ids: set[str]) -> None:
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


def _alternatives(recettes: list[dict], used_names: set[str]) -> list[dict]:
    """Recettes proposables en remplacement, hors celles déjà au planning.

    On expose la Nature pour que le picker distingue les recettes des
    « briques » (composants simples). On garde aussi les briques
    (Nature=Ingrédient) : réutilisables en plat (n'importe quelle Base) comme
    en accompagnement (Base=Légume)."""
    return [
        {"nom": r["nom"], "repas": recipe_types(r), "base": recipe_base(r),
         "tags": r["tags"], "nature": recipe_nature(r)}
        for r in recettes
        if r["nom"] not in used_names
        and (
            recipe_nature(r) == "Ingrédient"
            or not recipe_types(r)
            or set(recipe_types(r)) & {"Plat", "Entrée"}
            or "Légume" in recipe_base(r)
        )
    ]


def _brique_ingredients(nom: str, quantite: str, unite: str) -> list[dict]:
    """L'ingrédient de courses d'une brique est elle-même. On normalise via
    parse_ingredient_line pour découper qté/unité proprement, avec repli sur
    les valeurs saisies si le parsing ne renvoie rien d'exploitable."""
    parsed = parse_ingredient_line(" ".join(p for p in (quantite, unite, nom) if p))
    if parsed and parsed.get("nom"):
        return [parsed]
    return [{"nom": nom, "quantite": quantite, "unite": unite}]


def _place_brique(target: dict, slot: str, nom: str, page_id: str,
                  notion_url: str, base: list[str]) -> None:
    """Place une brique fraîchement créée dans le repas `target`, en plat
    (elle remplace le plat, comme /api/free-meal) ou en accompagnement (ajout
    en fin de liste, sans écraser les autres)."""
    if slot == "plat":
        _set_main(target, nom, {
            "id": page_id,
            "url": "",
            "notion_url": notion_url,
            "nature": "Ingrédient",
            "base": base,
            "repas": [],
            "tags": [],
        })
        return
    accs = plat_accompagnements(target)
    accs.append({
        "nom_recette": nom,
        "notion_id": page_id,
        "url": "",
        "notion_url": notion_url,
        "nature": "Ingrédient",
        "base": base,
    })
    target["accompagnements"] = accs
    target.pop("accompagnement", None)


async def _editable_planning(draft_id: str) -> dict | None:
    """Planning à mettre à jour par /construire (`draft_id` d'un planning
    existant, brouillon OU semaine validée rouverte via `/?planning=<id>`),
    sinon None. Une semaine validée reste validée : `update_planning` ne
    touche pas à la colonne `valide`."""
    try:
        did = int(draft_id) if draft_id else 0
    except (TypeError, ValueError):
        did = 0
    if did:
        return await db.get_planning_with_recipes(did)
    return None


def _merge_existing_data(existing: dict | None, data: dict) -> dict:
    """Données d'un planning ré-enregistré depuis le constructeur : la nouvelle
    grille remplace plats, courses et période, mais les autres clés de
    `data_json` (lien de partage, articles ajoutés à la main…) sont conservées.
    Les coches de courses ne survivent que pour un article encore présent."""
    try:
        old = json.loads(existing["data_json"]) if existing else {}
    except (json.JSONDecodeError, TypeError, KeyError):
        old = {}
    if not isinstance(old, dict):
        old = {}
    merged = {**old, **data}
    # Purge par clé d'article (liste + articles manuels, anciennes coches par
    # nom tolérées) : services.shopping importe ce module, d'où l'import local.
    from app.services.shopping import purge_orphans
    purge_orphans(merged)
    # Ces clés décrivent l'ancienne génération : elles ne valent plus.
    for k in ("creneaux_non_resolus", "briques_manquantes"):
        if k not in data:
            merged.pop(k, None)
    return merged


def _meal_label(plat: dict) -> str:
    """Nom court d'un repas : plat + accompagnements (« Poulet + Riz »)."""
    nom = clean_recipe_title(plat.get("nom_recette", ""))
    accs = plat_accompagnements(plat)
    if accs:
        nom += " + " + " + ".join(clean_recipe_title(a.get("nom_recette", "")) for a in accs)
    return nom


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


def _nb_jours(data: dict) -> int:
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


def _periode_saisie(raw) -> int:
    """Durée de la période envoyée par le constructeur (champ `nb_jours`),
    1..7, ou 0 si absente ou invalide (repli sur les cases)."""
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return n if 1 <= n <= 7 else 0


def _nb_jours_from_cases(cases: list[dict]) -> int:
    """Dernier jour de la période saisie dans le constructeur : les jours hors
    période sont envoyés « absents » (persons 0), donc dernier jour à convives.
    0 si aucune case active."""
    return max((c["jour"] for c in cases if c["persons"] > 0), default=0)


# ── Édition groupée des repas (POST /api/planning/{id}/meals) ────────────

MEAL_ACTIONS = ("set_main", "add_side", "remove_side")


def _parse_slots(raw) -> list[dict] | None:
    """Valide la liste `slots` du corps : [{jour 1-7, moment midi|soir,
    persons (optionnel, > 0)}]. Dédoublonne (jour, moment). None si vide ou
    si une entrée est invalide (on refuse tout plutôt que d'en ignorer)."""
    if not isinstance(raw, list) or not raw or len(raw) > 14:
        return None
    out: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for s in raw:
        if not isinstance(s, dict):
            return None
        jour, moment = s.get("jour"), s.get("moment")
        if isinstance(jour, bool) or not isinstance(jour, int) or not 1 <= jour <= 7:
            return None
        if moment not in ("midi", "soir"):
            return None
        if (jour, moment) in seen:
            continue
        seen.add((jour, moment))
        try:
            persons = int(s.get("persons") or 0)
        except (TypeError, ValueError):
            persons = 0
        out.append({"jour": jour, "moment": moment, "persons": max(0, persons)})
    return out


def _new_plat(planning: dict, data: dict, slot: dict, nom: str,
              recette: dict | None) -> dict:
    """Crée le plat d'un créneau encore vide (cas de l'ajout depuis une fiche
    recette) : convives du slot, sinon du jour (`per_day`), sinon du planning."""
    persons = slot["persons"] or _per_day_list(planning, data)[slot["jour"] - 1]
    item = {"nom": nom, "notion_id": recette["id"] if recette else "",
            "nature": recipe_nature(recette) if recette else "Recette",
            "libre": not recette}
    by_id = {recette["id"]: recette} if recette else {}
    return _build_plat(item, slot["jour"], slot["moment"], persons, by_id)


async def apply_meals_batch(planning: dict, data: dict, slots: list[dict],
                            action: str, nom: str, remplace: str = "") -> str | None:
    """Applique UNE action à plusieurs créneaux, EN MÉMOIRE (`data["plats"]`).

    Renvoie un message d'erreur (rien n'est persisté : l'appelant n'écrit pas)
    ou None si toutes les mutations ont réussi ; l'appelant régénère alors la
    liste de courses et écrit une seule fois.

    - set_main : remplace le plat principal (accompagnements remis à zéro) ;
      un créneau VIDE (ou « absent ») reçoit un nouveau plat ;
    - add_side : ajoute l'accompagnement `nom` (doublon ignoré) ; si `remplace`
      est fourni, retire d'abord cet accompagnement (« ↻ Changer ») ;
    - remove_side : retire l'accompagnement `nom`.
    Les actions d'accompagnement exigent un plat existant dans chaque créneau."""
    plats = data.setdefault("plats", [])
    by_slot = {(p.get("jour"), p.get("moment")): p for p in plats}
    if action != "set_main" and any((s["jour"], s["moment"]) not in by_slot for s in slots):
        return "Repas non trouvé dans le planning"

    if action == "set_main":
        recette = _index_by_name(await notion.get_all_recipes()).get(nom.lower().strip())
        off = set(data.get("off_meals") or [])
        for s in slots:
            target = by_slot.get((s["jour"], s["moment"]))
            if target is None:
                plats.append(_new_plat(planning, data, s, nom, recette))
                off.discard(f"{s['jour']}:{s['moment']}")
                continue
            _set_main(target, nom, recette)
            target["libre"] = not bool(recette)
            if s["persons"]:
                target["persons"] = s["persons"]
        plats.sort(key=lambda p: (p.get("jour", 0), p.get("moment") != "midi"))
        if "off_meals" in data:
            data["off_meals"] = sorted(off)
        if data.get("nb_jours"):
            data["nb_jours"] = _nb_jours(data)
        return None

    cle = nom.lower().strip()
    ancien = remplace.lower().strip()
    side = await _resolve_side(nom) if action == "add_side" else None
    for s in slots:
        target = by_slot[(s["jour"], s["moment"])]
        accs = plat_accompagnements(target)
        retire = cle if action == "remove_side" else ancien
        if retire:
            accs = [a for a in accs if a.get("nom_recette", "").lower().strip() != retire]
        if side and not any(a.get("nom_recette", "").lower().strip() == cle for a in accs):
            accs.append(dict(side))
        target["accompagnements"] = accs
        target.pop("accompagnement", None)
    return None
