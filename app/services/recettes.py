"""Règles métier des recettes : statistiques de la liste, données de la fiche
(ingrédients en cache, portions, nutrition, usage), formulaires d'ajout et
d'édition, ajout au planning et enrichissement par lot."""

import json
import logging
from datetime import date

from app.config import (
    BASE_OPTIONS,
    NATURE_OPTIONS,
    REPAS_OPTIONS,
    TAG_OPTIONS,
    recipe_base,
    recipe_nature,
    recipe_types,
)
from app.deps import BASE_SERVINGS, db, llm, notion
from app.nutrition import estimate_nutrition
from app.services.planning import (
    _attach_label,
    _day_labels,
    apply_meals_batch,
    plat_accompagnements,
)
from app.services.shopping import _ing_lines, _ingredients_to_text, _refresh_shopping_and_save
from app.text_utils import normalize_title_case, parse_ingredient_line, split_instructions

logger = logging.getLogger(__name__)

_NUTRITION_KEYS = ("calories", "proteines", "glucides", "lipides")

# Moment (select Notion) : valeur stockée → libellé du formulaire.
MOMENT_OPTIONS = [("Les deux", "Midi et soir"), ("Midi", "Midi uniquement"), ("Soir", "Soir uniquement")]

# État (status Notion) : options connues de la base (cf. RATED_STATUS).
ETAT_OPTIONS = ["À essayer", "Prochaine recette", "Réussie", "Testée"]


def _form_options() -> dict:
    """Valeurs proposées par les formulaires d'ajout et d'édition de recette."""
    return {
        "repas_options": REPAS_OPTIONS,
        "tag_options": TAG_OPTIONS,
        "base_options": BASE_OPTIONS,
        "nature_options": NATURE_OPTIONS,
        "moment_options": MOMENT_OPTIONS,
        "etat_options": ETAT_OPTIONS,
    }


def _recipe_stats(recettes: list[dict], ings_by_id: dict[str, str],
                  duree_by_id: dict[str, int]) -> dict:
    """Compteurs des filtres de la liste (type / base / état / tag / nature).

    Reporte aussi sur chaque recette ses ingrédients en cache (recherche par
    ingrédient) et sa durée (badge temps)."""
    par_type: dict[str, int] = {}
    par_base: dict[str, int] = {}
    par_etat: dict[str, int] = {}
    par_tag: dict[str, int] = {}
    par_nature: dict[str, int] = {}
    for r in recettes:
        r["ingredients_search"] = ings_by_id.get(r["id"], "")
        r["duree"] = duree_by_id.get(r["id"], 0)
        par_nature[recipe_nature(r)] = par_nature.get(recipe_nature(r), 0) + 1
        types = recipe_types(r) or ["Non classé"]
        for t in types:
            par_type[t] = par_type.get(t, 0) + 1
        for b in recipe_base(r):
            par_base[b] = par_base.get(b, 0) + 1
        if r["etat"]:
            par_etat[r["etat"]] = par_etat.get(r["etat"], 0) + 1
        for t in r["tags"]:
            par_tag[t] = par_tag.get(t, 0) + 1
    return {
        "total": len(recettes),
        "par_nature": par_nature,
        "par_type": sorted(par_type.items()),
        "par_base": sorted(par_base.items(), key=lambda kv: -kv[1]),
        "par_etat": sorted(par_etat.items()),
        "par_tag": sorted(par_tag.items(), key=lambda kv: -kv[1]),  # tags par fréquence
    }


def _cached_ingredients(cached: dict | None) -> list[dict]:
    """Ingrédients structurés {nom, quantite (texte), unite} du cache local."""
    if not cached or not cached.get("ingredients"):
        return []
    try:
        return [
            {"nom": i.get("nom", ""), "quantite": str(i.get("quantite", "") or ""),
             "unite": i.get("unite", "")}
            for i in json.loads(cached["ingredients"]) if i.get("nom")
        ]
    except (json.JSONDecodeError, TypeError):
        return []


def _detail_base_servings(cached: dict | None, recette: dict) -> int:
    """Base de portions d'une recette : priorité au cache, repli sur la valeur
    Notion (Portions) lue par _parse_page, défaut BASE_SERVINGS."""
    base_servings = BASE_SERVINGS
    try:
        if cached and cached.get("base_servings"):
            base_servings = int(cached["base_servings"])
        elif recette.get("base_servings"):
            base_servings = int(recette["base_servings"])
    except (TypeError, ValueError):
        base_servings = BASE_SERVINGS
    if base_servings <= 0:
        base_servings = BASE_SERVINGS
    return base_servings


def _detail_nutrition(cached: dict | None, ingredients: list[dict], base_servings: int) -> dict | None:
    """Nutrition par part : valeurs exactes de la source (JSON-LD) si on les a
    stockées, sinon estimation calculée depuis les ingrédients."""
    if cached and cached.get("nutrition"):
        try:
            src = json.loads(cached["nutrition"]) or {}
        except (json.JSONDecodeError, TypeError):
            src = {}
        # n'utiliser la nutrition de la source que si COMPLÈTE (calories +
        # 3 macros) ; sinon (souvent juste calories, parfois fausses) on estime
        if src and all(src.get(k) is not None for k in _NUTRITION_KEYS):
            src.setdefault("source", "source")
            src.setdefault("confiance", "Excellente")  # valeurs officielles du site
            return src
    if ingredients:
        return estimate_nutrition(ingredients, base_servings)
    return None


def _analysis_response(info: dict, image_url: str, source: str) -> dict:
    """Réponse des analyses d'URL ou de texte : champs pré-remplis du formulaire
    d'ajout (les ingrédients en lignes brutes sont joints en texte)."""
    ings = info.get("ingredients", [])
    ingredients = "\n".join(ings) if isinstance(ings, list) else str(ings)
    return {
        "nom": info.get("nom", ""),
        "repas": info.get("type_repas", ""),
        "tags": info.get("tags", []),
        "base": info.get("base", []),
        "ingredients": ingredients,
        "instructions": info.get("instructions", ""),
        "image_url": image_url,
        "moment": "Les deux",
        "source": source,
    }


def _planning_recipes(plats: list[dict]) -> list[dict]:
    """Recettes uniques d'un planning (plats + accompagnements, dédup par
    notion_id), au format attendu par _enrich_one : {id, nom, url}."""
    seen: set[str] = set()
    recettes: list[dict] = []
    for p in plats:
        for src in (p, *plat_accompagnements(p)):
            nid = src.get("notion_id")
            if nid and nid not in seen:
                seen.add(nid)
                recettes.append({"id": nid, "nom": src.get("nom_recette", ""),
                                 "url": src.get("url", "")})
    return recettes


async def _enrich_one(r: dict) -> str:
    """Enrichit une recette Notion. Retourne 'enriched' | 'skipped' | 'error'."""
    nid, nom = r.get("id"), r.get("nom")
    if not nid or not nom:
        return "skipped"
    # Normalise un titre tout en MAJUSCULES (best-effort, n'empêche pas l'enrichissement)
    nom_norm = normalize_title_case(nom)
    if nom_norm != nom:
        try:
            await notion.update_recipe_title(nid, nom_norm)
            nom = nom_norm
        except Exception as e:
            logger.warning(f"Normalisation titre échouée pour {nom}: {e}")
    ingredients_txt = ""
    cached = await db.get_enriched(nid)
    if cached and cached.get("ingredients"):
        try:
            ing_list = json.loads(cached["ingredients"])
            if ing_list:
                ingredients_txt = _ing_lines(ing_list)
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.warning(f"Cache ingrédients illisible pour {nom}: {e}")
    if not ingredients_txt and r.get("url"):
        try:
            d = await llm.extract_ingredients(nom, r.get("url", ""))
            ings = d.get("ingredients", [])
            if ings:
                ingredients_txt = _ing_lines(ings)
                await db.save_enriched(nid, nom, ingredients=json.dumps(ings))
        except Exception as e:
            logger.warning(f"Extraction LLM échouée pour {nom}: {e}")
    if not ingredients_txt:
        return "skipped"
    try:
        await notion.update_ingredients(nid, ingredients_txt)
        return "enriched"
    except Exception as e:
        logger.warning(f"Erreur écriture pour {nom}: {e}")
        return "error"


async def _enrich_many(recettes: list[dict]) -> dict[str, int]:
    """Enrichit une liste de recettes et compte les issues."""
    counts = {"enriched": 0, "skipped": 0, "errors": 0}
    for r in recettes:
        status = await _enrich_one(r)
        counts["errors" if status == "error" else status] += 1
    return counts


async def _enrich_all_events():
    """Événements SSE de l'enrichissement de tout le catalogue : progression
    après chaque recette, puis `done` (ou `error`)."""
    try:
        recettes = await notion.get_all_recipes()
        total = len(recettes)
        counts = {"enriched": 0, "skipped": 0, "errors": 0}
        yield f"data: {json.dumps({'total': total, 'done': 0})}\n\n"
        for i, r in enumerate(recettes, 1):
            status = await _enrich_one(r)
            counts["errors" if status == "error" else status] += 1
            payload = {"total": total, "done": i, "nom": r.get("nom", ""), "status": status, **counts}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield f"event: done\ndata: {json.dumps({'total': total, **counts})}\n\n"
    except Exception as e:
        logger.exception("Erreur enrichissement masse (stream)")
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"


# ── Usage des recettes (« Cuisinée N fois ») ─────────────────────────────


def _usage_label(usage: dict | None) -> str:
    """« Cuisinée 3 fois · dernière fois le 12/10 » (année ajoutée si ce n'est
    pas l'année en cours) ; chaîne vide si la recette n'a jamais été planifiée."""
    if not usage or not usage.get("count"):
        return ""
    out = f"Cuisinée {int(usage['count'])} fois"
    try:
        d = date.fromisoformat(usage.get("last") or "")
    except (TypeError, ValueError):
        return out
    fmt = f"{d.day:02d}/{d.month:02d}" + ("" if d.year == date.today().year else f"/{d.year}")
    return f"{out} · dernière fois le {fmt}"


def _attach_usage(recettes: list[dict], usage: dict[str, dict]) -> None:
    """Reporte sur chaque recette son usage (nombre, dernière date, libellé)."""
    for r in recettes:
        u = usage.get(r.get("id", "")) or {}
        r["usage_count"] = int(u.get("count") or 0)
        r["usage_last"] = u.get("last") or ""
        r["usage_label"] = _usage_label(u)


async def _recipe_usage() -> dict[str, dict]:
    """Usage de toutes les recettes, vide si la lecture échoue (best-effort)."""
    try:
        return await db.get_recipe_usage()
    except Exception as e:
        logger.warning(f"Lecture de l'usage des recettes échouée: {e}")
        return {}


# ── Édition d'une recette (formulaire « Modifier ») ──────────────────────


async def _enrichir_context(recette: dict, page_id: str, url: str = "", refetch: bool = False) -> dict:
    """Contexte du formulaire d'édition.

    Par défaut, pré-remplit depuis les données enregistrées (cache local pour
    les ingrédients, la durée, la nutrition et les portions ; blocs Notion pour
    les étapes) SANS appel à la source. L'extraction depuis l'URL n'a lieu que
    sur demande explicite (`refetch`, ou une URL fournie via « Extraire »), ou
    si la recette n'a aucun ingrédient en cache mais une URL."""
    cached = await db.get_enriched(page_id) or {}
    ingredients = _cached_ingredients(cached)
    instructions_text = ""
    image_url = ""
    nutrition_json = cached.get("nutrition") or ""
    duree_minutes = cached.get("cuisson_minutes") or 0

    # URL effective : celle fournie par « Extraire », sinon celle de la recette.
    source_url = url.strip() or recette.get("url") or ""
    extracted = False
    if source_url and (refetch or url.strip() or not ingredients):
        try:
            info = await llm.extract_recipe_from_url(source_url)
            src_ings = [i for i in (parse_ingredient_line(x) for x in info.get("ingredients", []))
                        if i and i["nom"]]
            if src_ings:
                ingredients = src_ings
            instructions_text = info.get("instructions", "")
            image_url = info.get("image_url", "")
            if info.get("nutrition"):
                nutrition_json = json.dumps(info["nutrition"])
            duree_minutes = info.get("duree_minutes") or duree_minutes
            extracted = True
        except Exception as e:
            logger.warning(f"Extraction depuis la source pour {page_id}: {e}")

    if not instructions_text:
        try:
            instructions_text = "\n".join(await notion.get_recipe_instructions(page_id))
        except Exception:
            instructions_text = ""

    return {
        "recette": recette,
        "ingredients": ingredients,
        "steps": split_instructions(instructions_text),
        "image_url": image_url,
        "nutrition_json": nutrition_json,
        "duree_minutes": duree_minutes,
        "base_servings": _detail_base_servings(cached, recette),
        **_form_options(),
        "source_url": source_url,
        "needs_url": not recette.get("url"),
        "extracted": extracted,
    }


def _positive(value, default: int) -> int:
    """Entier strictement positif soumis, sinon `default`."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


async def _enrichir_enregistrer(page_id: str, recette: dict, f: dict) -> dict | None:
    """Applique le formulaire d'édition à Notion et au cache local.

    `f` : champs du formulaire (nom, repas, tags, base, nature, moment, etat,
    ingredients_text, steps, image_url, nutrition_json, duree_minutes,
    base_servings, source_url ; moment et etat valent None s'ils n'ont pas
    été soumis). Retourne None si tout est enregistré, sinon le contexte du
    formulaire à ré-afficher avec la saisie et un message d'erreur précis."""
    nom_source = (f.get("nom") or "").strip() or recette["nom"]
    # Casse d'un titre crié en MAJUSCULES normalisée ; casse choisie respectée.
    nom_norm = normalize_title_case(nom_source) if nom_source.isupper() else nom_source
    source_url = (f.get("source_url") or "").strip()
    steps = [s.strip() for s in f.get("steps") or [] if s.strip()]
    structured = [
        i for i in (parse_ingredient_line(line) for line in (f.get("ingredients_text") or "").split("\n"))
        if i and i["nom"]
    ]
    nutrition_json = f.get("nutrition_json") or ""
    try:
        nutrition = nutrition_json if json.loads(nutrition_json) else ""
    except (json.JSONDecodeError, TypeError):
        nutrition = ""
    duree_minutes = f.get("duree_minutes") or 0
    bs = _positive(f.get("base_servings"), BASE_SERVINGS)
    image_url = f.get("image_url") or ""
    echecs: list[str] = []
    cache_local_ok = False

    async def _etape(label: str, coro) -> None:
        """Exécute une écriture Notion en isolant son échec éventuel."""
        try:
            await coro
        except Exception:
            logger.exception(f"Erreur validation enrichissement : {label}")
            echecs.append(label)

    if nom_norm != recette["nom"]:
        await _etape("titre", notion.update_recipe_title(page_id, nom_norm))
    if source_url and source_url != (recette.get("url") or ""):
        await _etape("URL source", notion.update_recipe_url(page_id, source_url))
    if structured:
        try:
            await db.save_enriched(page_id, nom_norm, ingredients=json.dumps(structured),
                                   cuisson_minutes=duree_minutes, nutrition=nutrition,
                                   base_servings=bs)
            cache_local_ok = True
        except Exception:
            logger.exception("Erreur validation enrichissement : cache local")
        await _etape("ingrédients", notion.update_ingredients(page_id, _ingredients_to_text(structured)))
    await _etape("portions", notion.update_portions(page_id, bs))
    if steps:
        await _etape("instructions", notion.rewrite_recipe_body(page_id, "\n".join(steps)))
    if image_url:
        await _etape("image", notion.update_image(page_id, image_url))
    meta: dict = {"repas": f.get("repas") or [], "tags": list(f.get("tags") or []),
                  "nature": f.get("nature") or "Recette", "base": f.get("base") or []}
    # Moment / État : écrits seulement s'ils font partie du formulaire soumis.
    for key in ("moment", "etat"):
        if f.get(key) is not None:
            meta[key] = f[key]
    await _etape("type/tags/base", notion.update_recipe_meta(page_id, **meta))

    if not echecs:
        return None
    msg = "Échec d'enregistrement dans Notion pour : " + ", ".join(echecs) + "."
    if structured:
        msg += (" Le cache local des ingrédients a bien été enregistré."
                if cache_local_ok else
                " Le cache local des ingrédients n'a pas non plus pu être enregistré.")
    msg += " Rien n'a été validé : corrige puis re-valide."
    # La saisie est reportée sur la recette pour re-cocher type/tags/nom.
    recette.update(nom=nom_norm, **{k: v for k, v in meta.items()})
    return {
        "recette": recette,
        "ingredients": structured,
        "steps": steps,
        "image_url": image_url,
        "nutrition_json": nutrition_json,
        "duree_minutes": duree_minutes,
        "base_servings": bs,
        **_form_options(),
        "source_url": source_url,
        "needs_url": not recette.get("url"),
        "error": msg,
    }


# ── Ajout d'une recette ──────────────────────────────────────────────────


async def _ajouter(f: dict) -> dict:
    """Crée une recette dans Notion depuis le formulaire d'ajout.

    Retourne le contexte de `ajouter.html` : `success`, `error` et, si la
    recette n'a pas pu être créée, `form` (toute la saisie) pour ré-afficher
    le formulaire à l'étape 2 sans rien perdre. Une page déjà créée dont une
    écriture secondaire a échoué n'est pas re-proposée (pas de doublon) : le
    message renvoie vers sa page « Modifier » (`page_id`)."""
    url = (f.get("url") or "").strip()
    nom = (f.get("nom") or "").strip()
    repas = list(f.get("repas") or [])
    tags = list(f.get("tags") or [])
    base = list(f.get("base") or [])
    steps = [s.strip() for s in f.get("steps") or [] if s.strip()]
    ingredients = f.get("ingredients_manual") or ""
    image_url = (f.get("image_url") or "").strip()
    bs = _positive(f.get("base_servings"), BASE_SERVINGS)
    nature = f.get("nature") or "Recette"
    moment = f.get("moment") or ""
    form = {
        "url": url, "nom": nom, "repas": repas, "tags": tags, "base": base, "nature": nature,
        "moment": moment or "Les deux", "base_servings": bs, "ingredients_manual": ingredients,
        "steps": steps, "image_url": image_url,
    }
    ctx: dict = {**_form_options(), "success": None, "error": None, "form": None, "page_id": ""}
    try:
        if url and not nom:
            extracted = await llm.extract_recipe_from_url(url)
            nom = extracted.get("nom", "")
            repas = repas or ([extracted["type_repas"]] if extracted.get("type_repas") else [])
            tags = tags or extracted.get("tags", [])
            base = base or extracted.get("base", [])
        if not nom:
            return {**ctx, "error": "Il faut au moins un nom ou une URL.", "form": form}
        result = await notion.create_recipe(
            nom=nom, url=url, repas=repas, tags=tags, moment=moment,
            nature=nature, base=base, base_servings=bs,
        )
    except Exception as e:
        logger.exception("Erreur ajout recette")
        return {**ctx, "error": f"Erreur : {e}", "form": form}

    page_id = result.get("id", "")
    logger.info(f"Recette ajoutée: {nom} → {result.get('url', '')}")
    echecs: list[str] = []

    async def _etape(label: str, coro) -> None:
        try:
            await coro
        except Exception as e:
            logger.warning(f"Impossible de sauvegarder {label}: {e}")
            echecs.append(label)

    if page_id and ingredients:
        await _etape("les ingrédients", notion.update_ingredients(page_id, ingredients))
    if page_id and steps:
        await _etape("les instructions", notion.append_instructions(page_id, "\n".join(steps)))
    if page_id and image_url:
        await _etape("l'image", notion.update_image(page_id, image_url))
    if page_id and ingredients:
        ings_list = [i for i in (parse_ingredient_line(line) for line in ingredients.split("\n"))
                     if i and i["nom"]]
        await _etape("le cache local des ingrédients", db.save_enriched(
            notion_id=page_id, recipe_name=nom, ingredients=json.dumps(ings_list), base_servings=bs,
        ))
    if echecs:
        return {**ctx, "page_id": page_id,
                "error": (f"Recette « {nom} » créée dans Notion, mais impossible d'enregistrer : "
                          + ", ".join(echecs) + ". Complète-la depuis sa page « Modifier ».")}
    return {**ctx, "page_id": page_id, "success": f"Recette « {nom} » ajoutée avec succès !"}


# ── Ajout au planning depuis la fiche ────────────────────────────────────

_MOMENT_ORDER = {"midi": 0, "soir": 1}


def _draft_choices(drafts: list[dict]) -> list[dict]:
    """Brouillons proposés par la modale « Ajouter au planning » : libellé,
    jours (dérivés du jour 1) et créneaux déjà occupés {"jour:moment": nom}."""
    out: list[dict] = []
    for d in drafts:
        _attach_label(d)
        try:
            data = json.loads(d.get("data_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            data = {}
        occupied = {f"{p.get('jour')}:{p.get('moment')}": p.get("nom_recette", "")
                    for p in data.get("plats", []) if isinstance(p, dict)}
        days = [{"jour": j["jour"], "label": f"{j['long']} {j['date']}".strip()}
                for j in _day_labels(d.get("week_start", ""))]
        out.append({"id": d["id"], "label": d["label"], "days": days, "occupied": occupied})
    return out


async def _draft_choices_safe() -> list[dict]:
    """Brouillons de la modale, liste vide si la base est illisible."""
    try:
        return _draft_choices(await db.list_drafts())
    except Exception as e:
        logger.warning(f"Lecture des brouillons échouée: {e}")
        return []


async def _new_draft(start: str, persons: int) -> int:
    """Crée un brouillon vide d'un jour démarrant le `start` (AAAA-MM-JJ)."""
    data = {"plats": [], "liste_courses": [], "per_day": ",".join([str(persons)] * 7),
            "off_meals": [], "builder": [], "nb_jours": 1}
    return await db.save_planning(
        week_start=start, saison="", nb_personnes=persons, ingredients_force="",
        data_json=json.dumps(data, ensure_ascii=False), recipes=[],
    )


async def _planifier(page_id: str, body: dict) -> dict:
    """Place une recette dans un brouillon : `planning_id` + `jour` (1-7) +
    `moment`, ou (`planning_id` absent ou « new ») un nouveau brouillon d'un
    jour démarrant à `date` (AAAA-MM-JJ, défaut aujourd'hui). Le plat du
    créneau est créé ou remplacé (même règle que POST /api/planning/{id}/meals,
    action set_main), puis la liste de courses et la grille du constructeur
    sont régénérées : une seule écriture du planning, sous son verrou.

    Le brouillon neuf n'est créé qu'une fois les paramètres et la recette
    validés, et il est supprimé si l'écriture échoue.

    Retourne {success, planning_id, url} ou {error}."""
    moment = body.get("moment")
    if moment not in _MOMENT_ORDER:
        return {"error": "Moment invalide (midi ou soir)"}
    nouveau = body.get("planning_id") in (None, "", "new")
    try:
        jour = 1 if nouveau else int(body.get("jour"))
        start = date.fromisoformat(body.get("date") or date.today().isoformat()) if nouveau else None
        planning_id = 0 if nouveau else int(body.get("planning_id"))
    except (TypeError, ValueError):
        return {"error": "Paramètres invalides (brouillon, jour 1 à 7, date AAAA-MM-JJ)"}
    if not 1 <= jour <= 7:
        return {"error": "Jour invalide (1 à 7)"}

    recette = await notion.get_recipe(page_id)
    if not recette:
        return {"error": "Recette introuvable"}

    slot = {"jour": jour, "moment": moment, "persons": _positive(body.get("persons"), 0)}
    if nouveau:
        planning_id = await _new_draft(start.isoformat(), _positive(body.get("persons"), BASE_SERVINGS))

    def mutate(planning: dict, data: dict) -> str | None:
        if planning.get("valide"):
            return "Brouillon introuvable"
        return apply_meals_batch(planning, data, [slot], "set_main", recette["nom"],
                                 resolved=recette)

    try:
        res = await _refresh_shopping_and_save(planning_id, mutate)
    except Exception:
        if nouveau:
            await db.delete_planning(planning_id)
        raise
    if res.get("error"):
        if nouveau:
            await db.delete_planning(planning_id)
        return {"error": "Brouillon introuvable" if res["error"] == "Planning introuvable"
                else res["error"]}
    planning = await db.get_planning_with_recipes(planning_id) or {}
    # Entrée à jour de la modale (créneaux occupés, nouveau brouillon).
    draft = _draft_choices([{"id": planning_id, "week_start": planning.get("week_start", ""),
                             "data_json": planning.get("data_json") or "{}"}])[0]
    return {"success": True, "planning_id": planning_id, "url": f"/?draft={planning_id}", "draft": draft}
