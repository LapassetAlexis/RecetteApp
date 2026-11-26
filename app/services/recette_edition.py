"""Ajout et édition d'une recette : réponse des analyses (URL, texte),
formulaire « Modifier » (pré-remplissage, enregistrement dans Notion et dans
le cache local) et formulaire d'ajout."""

import json
import logging

from app.deps import BASE_SERVINGS, db, llm, notion
from app.services.recettes import cached_ingredients, detail_base_servings, form_options, positive
from app.services.shopping import ingredients_to_text
from app.text_utils import normalize_title_case, parse_ingredient_lines, split_instructions

logger = logging.getLogger(__name__)

# Message affiché quand la création échoue ; le détail de l'exception va dans les logs.
AJOUT_IMPOSSIBLE = ("Ajout de la recette impossible : Notion indisponible ou URL illisible. "
                    "Ta saisie est conservée, réessaie.")


def analysis_response(info: dict, image_url: str, source: str) -> dict:
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


def parse_lines(lines) -> list[dict]:
    """Ingrédients structurés d'une saisie ligne à ligne : « sel, poivre »
    donne deux ingrédients (`parse_ingredient_lines`), lignes vides ignorées."""
    return [i for line in lines for i in parse_ingredient_lines(str(line or "")) if i.get("nom")]


# ── Édition d'une recette (formulaire « Modifier ») ──────────────────────


async def enrichir_context(recette: dict, page_id: str, url: str = "", refetch: bool = False) -> dict:
    """Contexte du formulaire d'édition.

    Par défaut, pré-remplit depuis les données enregistrées (cache local pour
    les ingrédients, la durée, la nutrition et les portions ; blocs Notion pour
    les étapes) SANS appel à la source. L'extraction depuis l'URL n'a lieu que
    sur demande explicite (`refetch`, ou une URL fournie via « Extraire »), ou
    si la recette n'a aucun ingrédient en cache mais une URL."""
    cached = await db.get_enriched(page_id) or {}
    ingredients = cached_ingredients(cached)
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
            src_ings = parse_lines(info.get("ingredients", []))
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
        "base_servings": detail_base_servings(cached, recette),
        **form_options(),
        "source_url": source_url,
        "needs_url": not recette.get("url"),
        "extracted": extracted,
    }


async def enrichir_enregistrer(page_id: str, recette: dict, f: dict) -> dict | None:
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
    structured = parse_lines((f.get("ingredients_text") or "").split("\n"))
    nutrition_json = f.get("nutrition_json") or ""
    try:
        nutrition = nutrition_json if json.loads(nutrition_json) else ""
    except (json.JSONDecodeError, TypeError):
        nutrition = ""
    duree_minutes = f.get("duree_minutes") or 0
    bs = positive(f.get("base_servings"), BASE_SERVINGS)
    image_url = f.get("image_url") or ""
    echecs: list[str] = []
    reussites: list[str] = []

    async def _etape(label: str, coro) -> None:
        """Exécute une écriture Notion en isolant son échec éventuel."""
        try:
            await coro
            reussites.append(label)
        except Exception:
            logger.exception(f"Erreur validation enrichissement : {label}")
            echecs.append(label)

    if nom_norm != recette["nom"]:
        await _etape("titre", notion.update_recipe_title(page_id, nom_norm))
    if source_url and source_url != (recette.get("url") or ""):
        await _etape("URL source", notion.update_recipe_url(page_id, source_url))
    # Cache local : portions, durée et nutrition écrites même sans ingrédient
    # (la base de portions du cache prime sur Notion) ; une liste
    # d'ingrédients vidée vide aussi le cache. Notion n'est pas vidé : le
    # formulaire n'affiche que le cache, un champ vide n'y vaut pas effacement.
    try:
        await db.save_enriched(page_id, nom_norm,
                               ingredients=json.dumps(structured) if structured else "",
                               cuisson_minutes=duree_minutes, nutrition=nutrition,
                               base_servings=bs)
        cache_local_ok = True
    except Exception:
        logger.exception("Erreur validation enrichissement : cache local")
        cache_local_ok = False
    if structured:
        await _etape("ingrédients", notion.update_ingredients(page_id, ingredients_to_text(structured)))
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
    if reussites:
        msg += " Bien enregistré dans Notion : " + ", ".join(reussites) + "."
    quoi = "des ingrédients" if structured else "(portions, durée)"
    msg += (f" Le cache local {quoi} a bien été enregistré." if cache_local_ok
            else f" Le cache local {quoi} n'a pas pu être enregistré.")
    msg += " Corrige puis re-valide pour réécrire ce qui a échoué."
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
        **form_options(),
        "source_url": source_url,
        "needs_url": not recette.get("url"),
        "error": msg,
    }


# ── Ajout d'une recette ──────────────────────────────────────────────────


async def ajouter(f: dict) -> dict:
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
    bs = positive(f.get("base_servings"), BASE_SERVINGS)
    nature = f.get("nature") or "Recette"
    moment = f.get("moment") or ""
    form = {
        "url": url, "nom": nom, "repas": repas, "tags": tags, "base": base, "nature": nature,
        "moment": moment or "Les deux", "base_servings": bs, "ingredients_manual": ingredients,
        "steps": steps, "image_url": image_url,
    }
    ctx: dict = {**form_options(), "success": None, "error": None, "form": None, "page_id": ""}
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
    except Exception:
        # Détail (exception Notion ou LLM) dans les logs, pas dans la page.
        logger.exception("Erreur ajout recette")
        return {**ctx, "error": AJOUT_IMPOSSIBLE, "form": form}

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
        ings_list = parse_lines(ingredients.split("\n"))
        await _etape("le cache local des ingrédients", db.save_enriched(
            notion_id=page_id, recipe_name=nom, ingredients=json.dumps(ings_list), base_servings=bs,
        ))
    if echecs:
        return {**ctx, "page_id": page_id,
                "error": (f"Recette « {nom} » créée dans Notion, mais impossible d'enregistrer : "
                          + ", ".join(echecs) + ". Complète-la depuis sa page « Modifier ».")}
    return {**ctx, "page_id": page_id, "success": f"Recette « {nom} » ajoutée avec succès !"}
