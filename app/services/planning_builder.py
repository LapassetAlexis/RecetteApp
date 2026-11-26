"""Grille du constructeur (`builder`) : lecture du champ `meals` envoyé par le
constructeur, et reconstruction de la grille depuis les plats d'un planning
(anciens plannings sans grille, modifications faites depuis la page planning),
groupes de midis compris."""

import json

from app.services.planning import plat_accompagnements


def parse_builder_meals(raw: str) -> list[dict]:
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


GROUPES_MAX = 5   # couleurs du constructeur (builder.js : GROUP_COLORS)


def _persons_of(p: dict, default: int) -> int:
    try:
        n = int(p.get("persons") or 0)
    except (TypeError, ValueError):
        n = 0
    return n if n > 0 else default


def _plat_signature(p: dict | None):
    """Identité d'un repas (plat + accompagnements), None pour un créneau vide."""
    if not p:
        return None
    return (
        str(p.get("notion_id", "") or ""), p.get("nom_recette", ""),
        tuple((str(a.get("notion_id", "") or ""), a.get("nom_recette", ""))
              for a in plat_accompagnements(p)),
    )


def _item_signature(main, accs) -> tuple | None:
    """Même identité qu'un plat, calculée sur une case du constructeur."""
    if not isinstance(main, dict) or not main.get("nom"):
        return None
    return (
        str(main.get("notion_id", "") or ""), main.get("nom", ""),
        tuple((str(a.get("notion_id", "") or ""), a.get("nom", ""))
              for a in (accs if isinstance(accs, list) else [])
              if isinstance(a, dict) and a.get("nom")),
    )


def _previous_cases(previous) -> dict[tuple[int, str], dict]:
    """Cases lisibles de l'ancienne grille : {(jour, moment): {group, persons, sig}}.

    Dans la grille, seul le premier midi actif d'un groupe porte le repas ; les
    autres midis actifs du groupe le partagent (`sig` = repas du groupe)."""
    out: dict[tuple[int, str], dict] = {}
    for c in previous if isinstance(previous, list) else []:
        if not isinstance(c, dict) or c.get("moment") not in ("midi", "soir"):
            continue
        try:
            jour = int(c.get("jour"))
            persons = max(0, int(c.get("persons") or 0))
            group = int(c.get("group")) if c["moment"] == "midi" else 0
        except (TypeError, ValueError):
            continue
        if 1 <= jour <= 7 and (jour, c["moment"]) not in out:
            out[(jour, c["moment"])] = {
                "group": group, "persons": persons,
                "sig": _item_signature(c.get("main"), c.get("accompagnements")),
            }
    repas: dict[int, tuple] = {}
    for jour in range(1, 8):
        m = out.get((jour, "midi"))
        if m and m["persons"] > 0 and m["sig"] is not None:
            repas.setdefault(m["group"], m["sig"])
    for jour in range(1, 8):
        m = out.get((jour, "midi"))
        if m:
            m["sig"] = repas.get(m["group"]) if m["persons"] > 0 else None
    return out


def _midi_groups(sigs: dict[int, tuple | None], actifs: set[int],
                 prev: dict[int, dict]) -> dict[int, int]:
    """Numéro de groupe de chaque midi (jours 1..7). Même couleur = même repas :
    deux midis actifs d'un même groupe ont toujours le même repas.

    - midi actif dont le repas n'a pas changé : garde son ancien groupe
      (s'il est entre 1 et GROUPES_MAX ; au-delà, les midis qui le partageaient
      reçoivent ensemble un nouveau numéro) ;
    - midis actifs consécutifs au même repas : même groupe ;
    - sinon le plus petit groupe libre entre 1 et GROUPES_MAX. Si un groupe par
      série ne tient pas dans GROUPES_MAX, les repas identiques non consécutifs
      partagent leur groupe et les midis sans repas un groupe commun ; on ne
      dépasse GROUPES_MAX que si cela ne suffit pas ;
    - midi inactif (absent, hors période) : un groupe sans midi actif, de
      préférence son ancien."""
    groups: dict[int, int] = {}
    repas_du_groupe: dict[int, tuple | None] = {}
    remap: dict[int, int] = {}

    def _garde(j: int, vides: bool = True) -> bool:
        return (j in prev and prev[j]["sig"] == sigs[j] and 1 <= prev[j]["group"] <= GROUPES_MAX
                and (vides or sigs[j] is not None))

    # Groupes nécessaires si chaque série de repas et chaque midi vide a le sien.
    gardes = {prev[j]["group"] for j in actifs if _garde(j)}
    series = sum(1 for j in actifs if not _garde(j) and (
        sigs[j] is None or not ((j - 1) in actifs and sigs[j - 1] == sigs[j])))
    serre = len(gardes) + series > GROUPES_MAX
    deja = [j for j in sorted(actifs) if _garde(j)]
    if serre:
        # Les midis vides ne gardent rien et passent en dernier : ils prennent
        # ce qui reste, puis partagent un groupe sans repas. Un repas présent
        # dans plusieurs anciens groupes n'en garde qu'un (le premier).
        premier: dict[tuple, int] = {}
        for j in deja:
            if sigs[j] is not None:
                premier.setdefault(sigs[j], prev[j]["group"])
        deja = [j for j in deja if sigs[j] is not None and premier[sigs[j]] == prev[j]["group"]]
    gardes = {prev[j]["group"] for j in deja}
    autres = [j for j in sorted(actifs) if j not in deja]
    if serre:
        autres.sort(key=lambda j: (sigs[j] is None, j))

    def _inchange(j: int) -> bool:
        return j in prev and prev[j]["sig"] == sigs[j]

    def _libre(exclure: set[int]) -> int | None:
        return next((g for g in range(1, GROUPES_MAX + 1) if g not in exclure), None)

    def _meme_repas(sig) -> int | None:
        return next((k for k, s in repas_du_groupe.items() if s == sig), None)

    for jour in deja + autres:
        sig = sigs[jour]
        old = prev.get(jour)
        g = old["group"] if jour in deja else None
        if g is None and _inchange(jour) and old["group"] > GROUPES_MAX:
            g = remap.get(old["group"])
        if g is not None and g in repas_du_groupe and repas_du_groupe[g] != sig:
            g = None   # ancienne grille incohérente : on ne mélange pas deux repas
        if g is None and sig is not None and (jour - 1) in groups and sigs[jour - 1] == sig:
            g = groups[jour - 1]
        if g is None and serre and sig is not None:
            g = _meme_repas(sig)
        if g is None:
            g = _libre(set(repas_du_groupe) | gardes)
        if g is None:
            g = _meme_repas(sig)     # midi vide : rejoint un groupe sans repas
        if g is None:
            g = max(GROUPES_MAX, *repas_du_groupe, *gardes) + 1
        if _inchange(jour) and old["group"] > GROUPES_MAX:
            remap.setdefault(old["group"], g)
        groups[jour] = g
        repas_du_groupe.setdefault(g, sig)

    utilises = set(repas_du_groupe)
    for jour in range(1, 8):
        if jour in groups:
            continue
        old = prev.get(jour)
        if old and 1 <= old["group"] <= GROUPES_MAX and old["group"] not in utilises:
            groups[jour] = old["group"]
        else:
            groups[jour] = (_libre(utilises | set(groups.values()))
                            or _libre(utilises) or groups.get(jour - 1) or 1)
    return groups


def builder_from_plats(plats: list[dict], off_meals: list[str] | None = None,
                       default_persons: int = 4, previous: list | None = None,
                       nb_jours: int | None = None) -> list[dict]:
    """Reconstruit la grille du constructeur (`builder`) depuis les plats.

    Sert aux anciens plannings sans `builder` et après une modification depuis
    la page planning (le constructeur rechargerait sinon l'ancienne grille).
    Une case par (jour, moment) sur 7 jours :
    - case avec plat : `persons` du plat (repli `default_persons`), plat
      principal + accompagnements ;
    - case sans plat : absente (persons 0) si elle est dans `off_meals` ou hors
      de la période (`nb_jours`, défaut : dernier jour porteur d'un plat) ;
      sinon vide, avec ses anciens convives (`previous`), ceux d'un plat du
      jour, ou à défaut `default_persons` ;
    - midis : voir `_midi_groups`. `previous` (l'ancienne grille) garde les
      groupes choisis dans le constructeur pour les midis inchangés ; sans
      elle, numérotation compacte (1, 2, 3…). Comme dans le constructeur,
      seul le premier midi actif d'un groupe porte le repas."""
    off = set(off_meals or [])
    by_slot: dict[tuple[int, str], dict] = {}
    for p in plats:
        try:
            key = (int(p.get("jour")), p.get("moment"))
        except (TypeError, ValueError):
            continue
        if 1 <= key[0] <= 7 and key[1] in ("midi", "soir"):
            by_slot.setdefault(key, p)
    if not nb_jours:
        nb_jours = max((j for j, _ in by_slot), default=0)
    prev = _previous_cases(previous)

    cases: dict[tuple[int, str], dict] = {}
    for jour in range(1, 8):
        day_persons = next((_persons_of(by_slot[(jour, m)], default_persons)
                            for m in ("midi", "soir") if (jour, m) in by_slot), 0)
        for moment in ("midi", "soir"):
            p = by_slot.get((jour, moment))
            case: dict = {"jour": jour, "moment": moment}
            if p:
                case["persons"] = _persons_of(p, default_persons)
                case["main"] = _builder_item(p)
            elif f"{jour}:{moment}" in off or jour > nb_jours:
                case["persons"] = 0
            else:
                ancien = prev.get((jour, moment), {}).get("persons", 0)
                case["persons"] = ancien or day_persons or default_persons
            case["accompagnements"] = ([_builder_item(a) for a in plat_accompagnements(p)]
                                       if p else [])
            cases[(jour, moment)] = case

    sigs = {j: _plat_signature(by_slot.get((j, "midi"))) for j in range(1, 8)}
    actifs = {j for j in range(1, 8) if cases[(j, "midi")]["persons"] > 0}
    prev_midis = {j: c for (j, m), c in prev.items() if m == "midi"}
    groups = _midi_groups(sigs, actifs, prev_midis)
    porteurs: set[int] = set()
    for jour in range(1, 8):
        case = cases[(jour, "midi")]
        case["group"] = groups[jour]
        if jour in actifs and groups[jour] in porteurs:
            case.pop("main", None)            # repas porté par le 1er midi du groupe
            case["accompagnements"] = []
        elif jour in actifs:
            porteurs.add(groups[jour])
    return [cases[(j, m)] for j in range(1, 8) for m in ("midi", "soir")]
