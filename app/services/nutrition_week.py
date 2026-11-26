"""Nutrition moyenne d'un planning, par jour et par personne."""

import json

from app.deps import BASE_SERVINGS, db
from app.nutrition import estimate_nutrition
from app.services.planning import plat_accompagnements


async def _week_nutrition(plats: list[dict], day_labels: list[dict] | None = None) -> dict | None:
    """Estime la nutrition moyenne par jour/personne sur le planning, à partir
    des ingrédients en cache. Retourne None si rien d'estimable."""
    ids: set[str] = set()
    for p in plats:
        if p.get("notion_id"):
            ids.add(p["notion_id"])
        for acc in plat_accompagnements(p):
            if acc.get("notion_id"):
                ids.add(acc["notion_id"])
    if not ids:
        return None

    per_recipe: dict[str, dict] = {}
    for nid in ids:
        cached = await db.get_enriched(nid)
        if not cached or not cached.get("ingredients"):
            continue
        try:
            ings = [
                {"nom": i.get("nom", ""), "quantite": str(i.get("quantite", "") or ""),
                 "unite": i.get("unite", "")}
                for i in json.loads(cached["ingredients"]) if i.get("nom")
            ]
        except (json.JSONDecodeError, TypeError):
            continue
        nut = None
        if cached.get("nutrition"):
            try:
                src = json.loads(cached["nutrition"]) or {}
                if src and all(src.get(k) is not None for k in ("calories", "proteines", "glucides", "lipides")):
                    nut = src
            except (json.JSONDecodeError, TypeError):
                nut = None
        if not nut:
            try:
                base = int(cached.get("base_servings") or BASE_SERVINGS)
            except (TypeError, ValueError):
                base = BASE_SERVINGS
            nut = estimate_nutrition(ings, base or BASE_SERVINGS)
        if nut:
            per_recipe[nid] = nut

    if not per_recipe:
        return None

    _KEYS = ("calories", "proteines", "glucides", "lipides")

    def _meal_nut(p: dict) -> dict | None:
        """Somme nutrition d'un repas (plat + tous ses accompagnements) ou None
        si non estimable."""
        recs = [p.get("notion_id")]
        for acc in plat_accompagnements(p):
            if acc.get("notion_id"):
                recs.append(acc["notion_id"])
        parts = [per_recipe[n] for n in recs if n in per_recipe]
        if not parts:
            return None
        return {k: sum((n.get(k, 0) or 0) for n in parts) for k in _KEYS}

    tot = {k: 0.0 for k in _KEYS}
    seen: set[tuple] = set()
    meals_total = meals_est = 0
    days: set[int] = set()
    # Détail par jour : {jour: {"midi": nut|None, "soir": nut|None}}
    by_day: dict[int, dict[str, dict | None]] = {}
    for p in plats:
        jour = p["jour"]
        days.add(jour)
        key = (jour, p["moment"])
        if key in seen:
            continue
        seen.add(key)
        meals_total += 1
        nut = _meal_nut(p)
        by_day.setdefault(jour, {})[p["moment"]] = nut
        if not nut:
            continue
        meals_est += 1
        for k in tot:
            tot[k] += nut[k]

    if not meals_est:
        return None
    ndays = len(days) or 1
    cov = meals_est / meals_total if meals_total else 0

    JOURS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

    def _jour_nom(jour: int) -> str:
        # Nom du jour dérivé de la DATE (day_labels) si dispo, sinon repli sur
        # l'index (compat : anciens appels sans week_start / plannings « lundi »).
        if day_labels and 1 <= jour <= len(day_labels):
            return day_labels[jour - 1]["long"]
        return JOURS[jour - 1] if 1 <= jour <= 7 else f"Jour {jour}"

    def _round(n: dict | None) -> dict | None:
        return {k: round(n[k]) for k in _KEYS} if n else None

    par_jour = []
    for jour in sorted(days):
        midi = by_day.get(jour, {}).get("midi")
        soir = by_day.get(jour, {}).get("soir")
        day_total = None
        present = [m for m in (midi, soir) if m]
        if present:
            day_total = {k: sum(m[k] for m in present) for k in _KEYS}
        par_jour.append({
            "jour": jour,
            "nom": _jour_nom(jour),
            "midi": _round(midi),
            "soir": _round(soir),
            "total": _round(day_total),
        })

    return {
        "calories": round(tot["calories"] / ndays),
        "proteines": round(tot["proteines"] / ndays),
        "glucides": round(tot["glucides"] / ndays),
        "lipides": round(tot["lipides"] / ndays),
        "meals_estimes": meals_est, "meals_total": meals_total,
        "confiance": "Bonne" if cov >= 0.7 else ("Moyenne" if cov >= 0.4 else "Mauvaise"),
        "par_jour": par_jour,
    }
