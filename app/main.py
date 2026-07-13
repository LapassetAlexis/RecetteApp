"""App FastAPI — Menu Planner avec génération IA."""

import base64
import binascii
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import (
    BASE_OPTIONS,
    NATURE_OPTIONS,
    REPAS_OPTIONS,
    TAG_GROUPS,
    TAG_OPTIONS,
    recipe_base,
    recipe_nature,
    recipe_types,
    settings,
)
from app.database import Database
from app.llm_client import LLMClient
from app.notion_client import NotionClient
from app.categories import RAYON_ORDER, categorize, group_by_rayon
from app.nutrition import estimate_nutrition
from app.text_utils import clean_recipe_title, merge_ingredients, normalize_cached_ingredient, normalize_title_case, parse_ingredient_line, scale_ingredients, split_instructions

VERSION = "1.1.0"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Instances
db = Database()
notion = NotionClient()
llm = LLMClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    logger.info("Base SQLite initialisée")
    try:
        await notion.ensure_ingredients_field()
    except Exception as e:
        logger.warning(f"Impossible de créer le champ Ingrédients Notion: {e}")
    yield


app = FastAPI(title=settings.app_title, version=VERSION, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)

# Auth HTTP Basic optionnelle, activée seulement si AUTH_USER + AUTH_PASSWORD
# sont définis. /health et /static restent publics (sondes, assets).
if settings.auth_enabled:
    # /courses/<token> : partage public en lecture seule, le token fait office
    # d'accès (pas d'auth Basic exigée sur ce préfixe).
    _PUBLIC_PREFIXES = ("/health", "/static", "/courses/")

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        if request.url.path.startswith(_PUBLIC_PREFIXES):
            return await call_next(request)

        unauthorized = Response(
            "Authentification requise",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Menu Planner"'},
        )

        header = request.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return unauthorized
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, password = decoded.partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return unauthorized

        # compare_digest : comparaison à temps constant (anti timing attack)
        ok_user = secrets.compare_digest(user, settings.auth_user)
        ok_pass = secrets.compare_digest(password, settings.auth_password)
        if not (ok_user and ok_pass):
            return unauthorized

        return await call_next(request)

    logger.info("🔒 Auth HTTP Basic activée")

# Static files & templates
BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# Version dans tous les templates
templates.env.globals["version"] = VERSION
# Identifiant d'asset (change à chaque démarrage) → casse le cache CSS/SW au
# redéploiement sans dépendre du SW pour rafraîchir.
templates.env.globals["asset_version"] = str(int(time.time()))
# Filtre de nettoyage des titres de recettes (retire les suffixes de site)
templates.env.filters["clean_title"] = clean_recipe_title
# Tags groupés par catégorie (affichage des formulaires)
templates.env.globals["tag_groups"] = TAG_GROUPS

# ── Pages ──────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Page d'accueil : constructeur manuel de planning (grille 7 jours × 2)."""
    today = date.today()
    # Calcul du lundi de la semaine
    lundi = today - timedelta(days=today.weekday())
    week_start = lundi.isoformat()

    # Dernier planning (lien « Voir »)
    dernier = await db.get_last_planning()
    planning_id = dernier["id"] if dernier else None

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "week_start": week_start,
            "planning_id": planning_id,
        },
    )


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


async def _week_nutrition(plats: list[dict]) -> dict | None:
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
            nut = estimate_nutrition(ings, BASE_SERVINGS)
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
            "nom": JOURS[jour - 1] if 1 <= jour <= 7 else f"Jour {jour}",
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


def _group_week(plats: list[dict]) -> dict[str, list[dict]]:
    """Regroupe les jours consécutifs partageant le même repas (plat + même
    accompagnement) en « runs » pour fusionner les cases du planning.
    Retourne {"midi": [run, ...], "soir": [...]} avec run = {start, span, jours, plat}."""
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
            else:
                run = {"key": key, "start": jour, "span": 1, "jours": [jour], "plat": plat}
                rows[moment].append(run)
    return rows


@app.get("/planning/{planning_id}", response_class=HTMLResponse)
async def voir_planning(request: Request, planning_id: int):
    """Affiche un planning existant."""
    planning = await db.get_planning_with_recipes(planning_id)
    if not planning:
        return RedirectResponse(url="/")

    data = json.loads(planning["data_json"])

    # Personnes par jour (anciens plannings : repli sur nb_personnes pour tous)
    per_day_raw = data.get("per_day", "")
    try:
        per_day = [int(x) for x in per_day_raw.split(",")] if per_day_raw else []
    except (ValueError, AttributeError):
        per_day = []
    if len(per_day) != 7:
        per_day = [planning.get("nb_personnes", 4)] * 7

    liste_courses = data.get("liste_courses", [])
    plats = data.get("plats", [])

    # Marque les repas dont la recette n'a pas d'ingrédients en cache
    # (« recette pas terminée ») pour afficher un avertissement dans le planning.
    enriched_ids = set(await db.get_all_enriched_ingredients())
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

    return templates.TemplateResponse(
        "planning.html",
        {
            "request": request,
            "planning": planning,
            "plats": plats,
            "week_rows": _group_week(plats),
            "week_nutrition": await _week_nutrition(plats),
            "liste_courses": liste_courses,
            "courses_par_rayon": group_by_rayon(liste_courses),
            "courses_checked": data.get("courses_checked", []),
            "rayon_order": RAYON_ORDER,
            "per_day": per_day,
            "off_meals": data.get("off_meals", []),
            "creneaux_non_resolus": data.get("creneaux_non_resolus", []),
            "briques_manquantes": data.get("briques_manquantes", ""),
            "valide": bool(planning.get("valide", 0)),
            "repas_options": REPAS_OPTIONS,
            "base_options": BASE_OPTIONS,
        },
    )


@app.post("/planning/{planning_id}/dupliquer")
async def dupliquer_planning(planning_id: int):
    """Recopie un planning existant en nouveau brouillon (sans liste de courses),
    daté de la semaine en cours. Permet de « refaire » une semaine appréciée."""
    src = await db.get_planning_with_recipes(planning_id)
    if not src:
        return {"error": "Planning introuvable"}
    data = json.loads(src["data_json"])
    plats = data.get("plats", [])
    today = date.today()
    week_start = (today - timedelta(days=today.weekday())).isoformat()

    await db.delete_draft_plannings()
    new_data = {"plats": plats, "liste_courses": [], "per_day": data.get("per_day", "")}
    new_id = await db.save_planning(
        week_start=week_start,
        saison=src.get("saison", ""),
        nb_personnes=src.get("nb_personnes", 4),
        ingredients_force=src.get("ingredients_force", ""),
        data_json=json.dumps(new_data, ensure_ascii=False),
        recipes=[{
            "notion_id": p.get("notion_id", ""), "recipe_name": p["nom_recette"],
            "repas_type": p.get("type_repas", ""), "jour": p["jour"], "moment": p["moment"],
        } for p in plats],
    )
    return {"success": True, "planning_id": new_id}


@app.post("/planning/{planning_id}/valider")
async def valider_planning(planning_id: int):
    """Valide un brouillon de planning : il rejoint l'historique."""
    planning = await db.get_planning_with_recipes(planning_id)
    if not planning:
        return {"error": "Planning introuvable"}
    await db.mark_planning_valid(planning_id)
    return {"success": True}


@app.get("/recettes", response_class=HTMLResponse)
async def liste_recettes(request: Request):
    """Liste toutes les recettes de la base Notion."""
    try:
        recettes = await notion.get_all_recipes()
    except Exception as e:
        logger.error(f"Erreur Notion: {e}")
        recettes = []

    # Ingrédients + durée en cache (recherche par ingrédient, badge temps)
    try:
        ings_by_id = await db.get_all_enriched_ingredients()
        duree_by_id = await db.get_all_enriched_durations()
    except Exception as e:
        logger.warning(f"Lecture cache recettes échouée: {e}")
        ings_by_id, duree_by_id = {}, {}

    # Stats pour les filtres (type / base / état / tag)
    total = len(recettes)
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

    return templates.TemplateResponse(
        "recettes.html",
        {
            "request": request,
            "recettes": recettes,
            "total": total,
            "par_nature": par_nature,
            "par_type": sorted(par_type.items()),
            "par_base": sorted(par_base.items(), key=lambda kv: -kv[1]),
            "par_etat": sorted(par_etat.items()),
            "par_tag": sorted(par_tag.items(), key=lambda kv: -kv[1]),  # tags par fréquence
            "repas_options": REPAS_OPTIONS,
            "tag_options": TAG_OPTIONS,
            "base_options": BASE_OPTIONS,
        },
    )


BASE_SERVINGS = 4  # base d'extraction des ingrédients (cf. extract_ingredients)

# État attribué à une recette dès qu'on la note (doit correspondre EXACTEMENT à
# une option « status » de Notion : To-do « À essayer », In progress
# « Prochaine recette », Complete « Réussie »/« Testée »). Noter = testé (pas
# forcément réussi), donc « Testée ».
RATED_STATUS = "Testée"


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


async def _collect_shopping(
    plats: list[dict], per_day: list[int], nb_personnes: int,
) -> tuple[list[dict], list[str]]:
    """Construit les ingrédients de courses ANCRÉS sur le cache DB par recette.

    - une recette contribue UNIQUEMENT ses ingrédients enregistrés (aucune
      extraction LLM à l'aveugle → pas d'ingrédients hallucinés) ;
    - les quantités sont mises à l'échelle selon le nb de personnes du REPAS
      (`plat.persons`), avec repli sur le nb du jour (`per_day`) puis
      `nb_personnes` pour les anciens plannings (× pers / BASE_SERVINGS) ;
    - chaque ingrédient porte le titre de sa recette source (`recette`).

    Retourne (ingrédients, titres des recettes non enrichies à signaler).
    """
    def _persons_for(src: dict) -> int:
        # Priorité au nb de personnes du repas (grille), sinon repli par jour.
        p = src.get("persons")
        try:
            if p is not None and int(p) > 0:
                return int(p)
        except (TypeError, ValueError):
            pass
        try:
            j = int(src.get("jour", 0))
        except (TypeError, ValueError):
            j = 0
        if per_day and 1 <= j <= len(per_day):
            return per_day[j - 1]
        return nb_personnes

    # Plats + accompagnements (chaque accompagnement hérite du jour ET du nb de
    # personnes de son plat).
    sources: list[dict] = []
    for p in plats:
        sources.append(p)
        for acc in plat_accompagnements(p):
            sources.append({**acc, "jour": p.get("jour"), "persons": p.get("persons")})

    collected: list[dict] = []
    non_enrichis: list[str] = []
    for src in sources:
        nid = src.get("notion_id", "")
        title = clean_recipe_title(src.get("nom_recette", ""))
        cached = await db.get_enriched(nid) if nid else None
        ings = None
        if cached and cached.get("ingredients"):
            try:
                ings = [i for i in json.loads(cached["ingredients"]) if i.get("nom")]
            except (json.JSONDecodeError, TypeError):
                ings = None
        if not ings:
            non_enrichis.append(title or src.get("nom_recette", ""))
            continue
        # Factorisation en aval : re-normalise (unités qui avaient fui dans le
        # nom sur d'anciens caches) et éclate les listes de condiments.
        clean: list[dict] = []
        for ing in ings:
            clean.extend(normalize_cached_ingredient(ing))
        factor = _persons_for(src) / BASE_SERVINGS
        for ing in scale_ingredients(clean, factor):
            ing["recette"] = title
            collected.append(ing)
    return collected, non_enrichis


async def _refresh_shopping_and_save(
    planning: dict, planning_id: int, planning_data: dict, plats: list[dict],
) -> dict:
    """Régénère la liste de courses (ancrée sur le cache DB), la range dans le
    planning, persiste et renvoie la réponse standard des endpoints planning.

    Mutualise la fin de /api/update-meal, /api/free-meal et /api/brique :
    mêmes règles (pas d'extraction LLM à l'aveugle, mise à l'échelle par jour,
    titre de recette par ingrédient, catégorisation par rayon)."""
    per_day = _per_day_list(planning, planning_data)
    collected, _ = await _collect_shopping(
        plats, per_day, planning.get("nb_personnes", 4),
    )
    liste_courses = merge_ingredients(collected)
    for it in liste_courses:
        it["rayon"] = categorize(it.get("nom", ""))
    planning_data["liste_courses"] = liste_courses

    await db.update_planning(
        planning_id=planning_id,
        data_json=json.dumps(planning_data, ensure_ascii=False),
        recipes=[
            {
                "notion_id": p.get("notion_id", ""),
                "recipe_name": p["nom_recette"],
                "repas_type": p.get("type_repas", ""),
                "jour": p["jour"],
                "moment": p["moment"],
            }
            for p in plats
        ],
    )
    return {"success": True, "liste_courses": liste_courses, "plats": plats}


@app.get("/recette/{page_id}", response_class=HTMLResponse)
async def detail_recette(request: Request, page_id: str):
    """Fiche détail : ingrédients (Cooklang, portions ajustables) + instructions."""
    try:
        recette = await notion.get_recipe(page_id)  # 1 appel, pas toute la base
        if not recette:
            return HTMLResponse("Recette non trouvée", status_code=404)

        # Ingrédients structurés depuis le cache local
        ingredients: list[dict] = []
        cached = await db.get_enriched(page_id)
        if cached and cached.get("ingredients"):
            try:
                ingredients = [
                    {"nom": i.get("nom", ""), "quantite": str(i.get("quantite", "") or ""),
                     "unite": i.get("unite", "")}
                    for i in json.loads(cached["ingredients"]) if i.get("nom")
                ]
            except (json.JSONDecodeError, TypeError):
                ingredients = []

        # Instructions depuis les blocks Notion (best-effort)
        try:
            instructions = await notion.get_recipe_instructions(page_id)
        except Exception as e:
            logger.warning(f"Instructions illisibles pour {page_id}: {e}")
            instructions = []

        # Nutrition par part : valeurs exactes de la source (JSON-LD) si on les a
        # stockées, sinon estimation calculée depuis les ingrédients.
        nutrition = None
        if cached and cached.get("nutrition"):
            try:
                src = json.loads(cached["nutrition"]) or {}
            except (json.JSONDecodeError, TypeError):
                src = {}
            # n'utiliser la nutrition de la source que si COMPLÈTE (calories +
            # 3 macros) ; sinon (souvent juste calories, parfois fausses) on estime
            if src and all(src.get(k) is not None for k in ("calories", "proteines", "glucides", "lipides")):
                src.setdefault("source", "source")
                src.setdefault("confiance", "Excellente")  # valeurs officielles du site
                nutrition = src
        if not nutrition and ingredients:
            nutrition = estimate_nutrition(ingredients, BASE_SERVINGS)

        duree = cached.get("cuisson_minutes") if cached else 0
        return templates.TemplateResponse(
            "recette_detail.html",
            {
                "request": request,
                "recette": recette,
                "ingredients": ingredients,
                "base_servings": BASE_SERVINGS,
                "instructions": instructions,
                "nutrition": nutrition,
                "duree": duree or 0,
            },
        )
    except Exception:
        logger.exception("Erreur détail recette")
        return HTMLResponse("Erreur interne lors du chargement de la recette.", status_code=500)


@app.get("/recette/{page_id}/enrichir", response_class=HTMLResponse)
async def enrichir_page(request: Request, page_id: str, url: str = ""):
    """Étape 1 : re-fetch la source et pré-remplit un formulaire éditable
    (ingrédients structurés + instructions nettoyées) à valider.
    Si la recette n'a pas d'URL, on peut en fournir une via ?url= pour extraire."""
    recette = await notion.get_recipe(page_id)
    if not recette:
        return HTMLResponse("Recette non trouvée", status_code=404)

    ingredients: list[dict] = []
    instructions_text = ""
    image_url = ""
    nutrition_src: dict = {}
    duree_minutes = 0

    # URL effective : celle de la recette, sinon celle fournie en paramètre.
    source_url = recette.get("url") or url.strip()
    needs_url = not recette.get("url")

    # « Enrichir » = re-fetch la SOURCE en priorité (données d'origine), pas le
    # cache qui peut contenir d'anciennes extractions inexactes.
    if source_url:
        try:
            info = await llm.extract_recipe_from_url(source_url)
            ingredients = [i for i in (parse_ingredient_line(x) for x in info.get("ingredients", [])) if i and i["nom"]]
            instructions_text = info.get("instructions", "")
            image_url = info.get("image_url", "")
            nutrition_src = info.get("nutrition") or {}
            duree_minutes = info.get("duree_minutes") or 0
        except Exception as e:
            logger.warning(f"Re-extraction enrichir {page_id}: {e}")

    # Repli sur le cache / les blocks Notion si la source n'a rien donné
    if not ingredients:
        cached = await db.get_enriched(page_id)
        if cached and cached.get("ingredients"):
            try:
                ingredients = json.loads(cached["ingredients"])
            except (json.JSONDecodeError, TypeError):
                ingredients = []
    if not instructions_text:
        try:
            instructions_text = "\n".join(await notion.get_recipe_instructions(page_id))
        except Exception:
            instructions_text = ""

    return templates.TemplateResponse(
        "enrichir.html",
        {
            "request": request,
            "recette": recette,
            "ingredients": ingredients,
            "steps": split_instructions(instructions_text),
            "image_url": image_url,
            "nutrition_json": json.dumps(nutrition_src),
            "duree_minutes": duree_minutes,
            "repas_options": REPAS_OPTIONS,
            "tag_options": TAG_OPTIONS,
            "base_options": BASE_OPTIONS,
            "nature_options": NATURE_OPTIONS,
            "source_url": source_url,
            "needs_url": needs_url,
        },
    )


@app.post("/recette/{page_id}/enrichir")
async def enrichir_submit(
    request: Request,
    page_id: str,
    nom: str = Form(""),
    repas: list[str] = Form([]),
    tags: list[str] = Form([]),
    base: list[str] = Form([]),
    nature: str = Form("Recette"),
    ingredients_text: str = Form(""),
    steps: list[str] = Form([]),
    image_url: str = Form(""),
    nutrition_json: str = Form(""),
    duree_minutes: int = Form(0),
    source_url: str = Form(""),
):
    """Étape 2 : applique les changements validés à la recette Notion + cache."""
    recette = await notion.get_recipe(page_id)
    if not recette:
        return RedirectResponse(url="/recettes", status_code=303)

    # Renommage libre : on prend le nom soumis s'il y en a un, sinon le nom
    # actuel. On normalise la casse d'un titre crié en MAJUSCULES (ex. import),
    # mais on respecte une casse choisie par l'utilisateur (renommage libre).
    nom_saisi = nom.strip()
    nom_source = nom_saisi or recette["nom"]
    nom_norm = normalize_title_case(nom_source) if nom_source.isupper() else nom_source

    # URL source : si la recette n'en avait pas (ou différente), on l'enregistre.
    source_url = source_url.strip()

    structured = [
        i for i in (parse_ingredient_line(l) for l in ingredients_text.split("\n")) if i and i["nom"]
    ]
    instructions_text = "\n".join(s.strip() for s in steps if s.strip())
    # Nutrition exacte de la source si fournie (sinon vide → estimée à l'affichage)
    nutrition = ""
    try:
        if nutrition_json and json.loads(nutrition_json):
            nutrition = nutrition_json
    except (json.JSONDecodeError, TypeError):
        nutrition = ""
    # On isole CHAQUE écriture Notion pour remonter précisément ce qui a échoué
    # (au lieu d'avaler tout dans un seul try/except et de rediriger en « succès »).
    echecs: list[str] = []          # étapes Notion ratées (label FR)
    cache_local_ok = False          # le cache local (ingrédients) a-t-il été sauvé ?

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
        # Cache local d'abord (distinct de Notion) : on note s'il a réussi.
        try:
            await db.save_enriched(page_id, nom_norm, ingredients=json.dumps(structured),
                                   cuisson_minutes=duree_minutes or 0, nutrition=nutrition)
            cache_local_ok = True
        except Exception:
            logger.exception("Erreur validation enrichissement : cache local")
        await _etape("ingrédients", notion.update_ingredients(page_id, _ingredients_to_text(structured)))
    if instructions_text:
        await _etape("instructions", notion.rewrite_recipe_body(page_id, instructions_text))
    if image_url:
        await _etape("image", notion.update_image(page_id, image_url))
    await _etape("type/tags/base", notion.update_recipe_meta(
        page_id, repas=repas, tags=tags or None, nature=nature, base=base))

    if echecs:
        # Au moins une écriture Notion a échoué : on NE redirige PAS en succès.
        # On re-affiche le formulaire en conservant toute la saisie + un message
        # d'erreur précis (étapes ratées et sort du cache local).
        msg = "Échec d'enregistrement dans Notion pour : " + ", ".join(echecs) + "."
        if structured:
            msg += (" Le cache local des ingrédients a bien été enregistré."
                    if cache_local_ok else
                    " Le cache local des ingrédients n'a pas non plus pu être enregistré.")
        msg += " Rien n'a été validé : corrige puis re-valide."
        # On reflète la saisie dans la recette pour re-cocher type/tags/nom.
        recette["nom"] = nom_norm
        recette["repas"] = repas or []
        recette["tags"] = tags or []
        recette["base"] = base or []
        recette["nature"] = nature or "Recette"
        return templates.TemplateResponse(
            "enrichir.html",
            {
                "request": request,
                "recette": recette,
                "ingredients": structured,
                "steps": [s.strip() for s in steps if s.strip()],
                "image_url": image_url,
                "nutrition_json": nutrition_json,
                "duree_minutes": duree_minutes,
                "repas_options": REPAS_OPTIONS,
                "tag_options": TAG_OPTIONS,
                "base_options": BASE_OPTIONS,
                "nature_options": NATURE_OPTIONS,
                "source_url": source_url,
                "needs_url": not recette.get("url"),
                "error": msg,
            },
        )

    return RedirectResponse(url=f"/recette/{page_id}", status_code=303)


@app.post("/recette/{page_id}/supprimer")
async def supprimer_recette(page_id: str):
    """Supprime une recette : archive la page Notion + purge le cache local.
    Renvoie du JSON (appelée en fetch depuis la fiche détail)."""
    try:
        await notion.archive_recipe(page_id)
        await db.delete_enriched(page_id)
    except Exception:
        logger.exception("Erreur suppression recette")
        return {"error": "Impossible de supprimer la recette."}
    return {"success": True}


@app.get("/ajouter", response_class=HTMLResponse)
async def ajouter_page(request: Request):
    """Page pour ajouter une recette depuis une URL."""
    return templates.TemplateResponse(
        "ajouter.html",
        {
            "request": request,
            "repas_options": REPAS_OPTIONS,
            "tag_options": TAG_OPTIONS,
            "base_options": BASE_OPTIONS,
            "nature_options": NATURE_OPTIONS,
            "success": None,
            "error": None,
        },
    )


@app.get("/historique", response_class=HTMLResponse)
async def historique(request: Request):
    """Liste les plannings précédents."""
    plannings = await db.list_plannings(limit=20)
    return templates.TemplateResponse(
        "historique.html",
        {"request": request, "plannings": plannings},
    )


# ── Actions ────────────────────────────────────────────────────────


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
        return {
            "notion_id": str(v.get("notion_id", "") or ""),
            "nom": nom,
            "nature": v.get("nature") or "Recette",
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
    }
    r = by_id.get(item.get("notion_id", ""))
    if r:
        _apply_recipe_info(plat, r)
    return plat


def _build_side(item: dict, by_id: dict[str, dict]) -> dict:
    """Construit un accompagnement (dict standard) depuis un item du catalogue."""
    side: dict = {
        "nom_recette": item["nom"],
        "notion_id": item.get("notion_id", "") or "",
        "url": "",
        "notion_url": "",
    }
    r = by_id.get(item.get("notion_id", ""))
    if r:
        side.update(
            notion_id=r["id"],
            url=r.get("url", ""),
            notion_url=r.get("notion_url", ""),
            nature=recipe_nature(r),
            base=recipe_base(r),
        )
    return side


@app.get("/api/catalogue")
async def api_catalogue():
    """Catalogue complet des recettes Notion pour le picker du constructeur.
    Renvoie [{id, nom, nature, base:[...], repas:[...]}] pour toutes les recettes."""
    try:
        recettes = await notion.get_all_recipes()
    except Exception as e:
        logger.error(f"Erreur Notion (catalogue): {e}")
        return []
    from app.text_utils import clean_recipe_title
    return [
        {
            "id": r["id"],
            "nom": clean_recipe_title(r["nom"]),
            "nature": recipe_nature(r),
            "base": recipe_base(r),
            "repas": recipe_types(r),
            "image": r.get("image", ""),
        }
        for r in recettes
    ]


@app.post("/construire")
async def construire(
    request: Request,
    week_start: str = Form(...),
    meals: str = Form(""),
):
    """Construit un planning À LA MAIN depuis la grille du formulaire.

    Le champ caché `meals` (JSON) porte les cases de la semaine :
    [{jour 1-7, moment midi|soir, persons (0 = absent), group (midis),
      main {notion_id, nom, nature} | null,
      accompagnements [{notion_id, nom}, ...]}].
    Cases sans `main` ou avec persons <= 0 → ignorées. Les midis d'un même
    groupe partagent le repas du 1er midi rempli du groupe (même couleur =
    même plat). On sauvegarde un BROUILLON + sa liste de courses, puis on
    redirige vers la page planning."""

    def _error_ctx(message: str) -> dict:
        return {"request": request, "error": message, "week_start": week_start}

    try:
        cases = _parse_builder_meals(meals)
        actives = [c for c in cases if c["persons"] > 0]

        # Catalogue Notion indexé par id (report des infos : base, nature, url…).
        recettes_all = await notion.get_all_recipes()
        by_id = {r["id"]: r for r in recettes_all}

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

        if not plats:
            return templates.TemplateResponse(
                "index.html",
                _error_ctx("Aucun repas renseigné. Choisis au moins un plat principal."),
            )

        # Personnes de référence + per_day (repli d'affichage / anciens plannings).
        nb_personnes = max((c["persons"] for c in actives), default=4)
        by_key = {(c["jour"], c["moment"]): c for c in cases}
        per_day_list = []
        for d in range(1, 8):
            pm = by_key.get((d, "midi"), {}).get("persons", 0)
            ps = by_key.get((d, "soir"), {}).get("persons", 0)
            per_day_list.append(max(pm, ps) or nb_personnes)
        per_day = ",".join(str(x) for x in per_day_list)
        off_meals = sorted(
            f"{c['jour']}:{c['moment']}" for c in cases if c["persons"] <= 0
        )

        # Liste de courses ANCRÉE sur le cache DB (mêmes règles que le reste de
        # l'app : pas d'extraction LLM à l'aveugle, mise à l'échelle PAR REPAS).
        collected, _ = await _collect_shopping(plats, per_day_list, nb_personnes)
        liste_courses = merge_ingredients(collected)
        for it in liste_courses:
            it["rayon"] = categorize(it.get("nom", ""))

        # Sauvegarde en BROUILLON (valide=0). On purge l'éventuel brouillon
        # précédent non validé.
        await db.delete_draft_plannings()
        data = {
            "plats": plats,
            "liste_courses": liste_courses,
            "per_day": per_day,
            "off_meals": off_meals,
        }
        planning_id = await db.save_planning(
            week_start=week_start,
            saison="",
            nb_personnes=nb_personnes,
            ingredients_force="",
            data_json=json.dumps(data, ensure_ascii=False),
            recipes=[{
                "notion_id": p.get("notion_id", ""),
                "recipe_name": p["nom_recette"],
                "repas_type": p.get("type_repas", ""),
                "jour": p["jour"],
                "moment": p["moment"],
            } for p in plats],
        )
        return RedirectResponse(url=f"/planning/{planning_id}", status_code=303)

    except Exception as e:
        logger.exception("Erreur lors de la construction du planning")
        return templates.TemplateResponse("index.html", _error_ctx(f"Erreur : {str(e)}"))


@app.post("/generate-shopping/{planning_id}")
async def generate_shopping(planning_id: int, request: Request):
    """Génère la liste de courses pour un planning existant."""
    try:
        planning = await db.get_planning_with_recipes(planning_id)
        if not planning:
            return {"error": "Planning introuvable"}

        planning_data = json.loads(planning["data_json"])
        plats = planning_data.get("plats", [])

        if not plats:
            return {"error": "Aucun plat dans ce planning"}

        # Ingrédients ANCRÉS sur le cache DB (pas d'extraction LLM à l'aveugle),
        # mis à l'échelle selon le nb de personnes de chaque jour.
        per_day = _per_day_list(planning, planning_data)
        collected, non_enrichis = await _collect_shopping(
            plats, per_day, planning["nb_personnes"],
        )
        # Fusion déterministe (dédup + addition par nom+unité, union des recettes).
        liste_courses = merge_ingredients(collected)

        # Ajouter les ingrédients forcés
        force = planning.get("ingredients_force", "")
        if force:
            for f in [i.strip() for i in force.split(",") if i.strip()]:
                if f.lower() not in {i["nom"].lower() for i in liste_courses}:
                    liste_courses.append({"nom": f, "quantite": "", "unite": "", "recettes": []})

        # Rayon de magasin pour chaque ingrédient (pour le groupement à l'affichage)
        for it in liste_courses:
            it["rayon"] = categorize(it.get("nom", ""))

        # Sauvegarder la liste de courses dans le planning.
        # NB : on n'écrit PAS cette liste dans Notion par recette — c'est une
        # liste agrégée/dédupliquée de la semaine, pas les ingrédients d'une
        # recette donnée (ceux-ci sont gérés à l'ajout / via enrich-all).
        planning_data["liste_courses"] = liste_courses
        # Nouveaux items : on repart d'une liste vierge de cases cochées.
        # (Le share_token éventuel est conservé tel quel.)
        planning_data["courses_checked"] = []
        await db.update_planning_data(planning_id, json.dumps(planning_data, ensure_ascii=False))

        return {
            "success": True,
            "liste_courses": liste_courses,
            "non_enrichis": sorted(set(non_enrichis)),
        }

    except Exception as e:
        logger.exception("Erreur génération liste courses")
        return {"error": str(e)}


@app.post("/api/shopping-check/{planning_id}")
async def shopping_check(planning_id: int, request: Request):
    """Coche/décoche un item de la liste de courses côté serveur.

    Body JSON : {"item": "<nom en minuscules>", "checked": true|false}.
    L'état est persisté dans planning_data["courses_checked"] (liste de clés)."""
    try:
        planning = await db.get_planning_with_recipes(planning_id)
        if not planning:
            return {"error": "Planning introuvable"}

        body = await request.json()
        item = str(body.get("item", "")).strip().lower()
        checked = bool(body.get("checked"))
        if not item:
            return {"error": "Item manquant"}

        planning_data = json.loads(planning["data_json"])
        # set pour dédupliquer, on repart de l'existant
        checked_set = set(planning_data.get("courses_checked", []))
        if checked:
            checked_set.add(item)
        else:
            checked_set.discard(item)
        planning_data["courses_checked"] = sorted(checked_set)
        await db.update_planning_data(planning_id, json.dumps(planning_data, ensure_ascii=False))
        return {"success": True}

    except Exception as e:
        logger.exception("Erreur mise à jour case liste de courses")
        return {"error": str(e)}


@app.post("/planning/{planning_id}/partager")
async def partager_liste(planning_id: int):
    """Génère (une fois) un token de partage public et renvoie l'URL relative."""
    try:
        planning = await db.get_planning_with_recipes(planning_id)
        if not planning:
            return {"error": "Planning introuvable"}

        planning_data = json.loads(planning["data_json"])
        token = planning_data.get("share_token")
        if not token:
            token = secrets.token_urlsafe(8)
            planning_data["share_token"] = token
            await db.update_planning_data(planning_id, json.dumps(planning_data, ensure_ascii=False))
        return {"url": f"/courses/{token}"}

    except Exception as e:
        logger.exception("Erreur génération lien de partage")
        return {"error": str(e)}


@app.get("/courses/{token}", response_class=HTMLResponse)
async def courses_public(request: Request, token: str):
    """Liste de courses publique (lecture seule) : n'affiche que les items NON
    cochés, groupés par rayon. Accès par token (exempté du basic-auth)."""
    planning = await db.get_planning_by_share_token(token)
    if not planning:
        return HTMLResponse("Liste introuvable", status_code=404)

    data = json.loads(planning["data_json"])
    liste_courses = data.get("liste_courses", [])
    checked = {str(c).lower() for c in data.get("courses_checked", [])}
    restants = [it for it in liste_courses if it.get("nom", "").lower() not in checked]

    return templates.TemplateResponse(
        "courses_public.html",
        {
            "request": request,
            "courses_par_rayon": group_by_rayon(restants),
            "nb_restants": len(restants),
            "nb_total": len(liste_courses),
        },
    )


@app.post("/api/rate/{page_id}")
async def api_rate(page_id: str, request: Request):
    """Note une recette (⭐ à ⭐⭐⭐⭐⭐)."""
    try:
        data = await request.json()
        note = data.get("note", "")
        # "" = retirer la note ; sinon une des 5 valeurs étoilées
        if note != "" and note not in ("⭐", "⭐⭐", "⭐⭐⭐", "⭐⭐⭐⭐", "⭐⭐⭐⭐⭐"):
            return {"error": "Note invalide"}
        await notion.update_rating(page_id, note)
        # Noter une recette = elle a été testée → on la sort de « À essayer ».
        warning = ""
        if note:
            try:
                await notion.update_status(page_id, RATED_STATUS)
            except Exception as e:
                logger.warning(f"Passage à « {RATED_STATUS} » échoué pour {page_id}: {e}")
                warning = (f"Note enregistrée, mais l'état n'a pas pu passer à "
                           f"« {RATED_STATUS} » (option Notion introuvable ?).")
        return {"success": True, "warning": warning}
    except Exception as e:
        logger.exception("Erreur notation")
        return {"error": str(e)}


@app.post("/api/analyze-url")
async def api_analyze_url(request: Request):
    """Analyse une URL et retourne les infos extraites."""
    try:
        data = await request.json()
        url = data.get("url", "")
        if not url:
            return {"error": "URL manquante"}

        # Extraction unifiée : titre, type, tags, ingrédients, instructions,
        # image — depuis la page elle-même (JSON-LD si dispo, sinon LLM).
        # Plus de second appel "à l'aveugle" qui hallucinait les ingrédients.
        info = await llm.extract_recipe_from_url(url)

        # Les ingrédients sortent en liste de lignes brutes → texte pour le form
        ings = info.get("ingredients", [])
        ingredients = "\n".join(ings) if isinstance(ings, list) else str(ings)

        return {
            "nom": info.get("nom", ""),
            "repas": info.get("type_repas", ""),
            "tags": info.get("tags", []),
            "base": info.get("base", []),
            "ingredients": ingredients,
            "instructions": info.get("instructions", ""),
            "image_url": info.get("image_url", ""),
            "moment": "Les deux",
            "source": info.get("source", ""),
        }

    except Exception as e:
        logger.exception("Erreur analyse URL")
        return {"error": str(e)}


@app.post("/api/analyze-text")
async def api_analyze_text(request: Request):
    """Structure une recette collée en texte libre (Gemini, note perso...)."""
    try:
        data = await request.json()
        text = data.get("text", "")
        if not text or not text.strip():
            return {"error": "Texte manquant"}

        info = await llm.extract_recipe_from_text(text)
        ings = info.get("ingredients", [])
        ingredients = "\n".join(ings) if isinstance(ings, list) else str(ings)

        return {
            "nom": info.get("nom", ""),
            "repas": info.get("type_repas", ""),
            "tags": info.get("tags", []),
            "base": info.get("base", []),
            "ingredients": ingredients,
            "instructions": info.get("instructions", ""),
            "image_url": "",
            "moment": "Les deux",
            "source": info.get("source", "llm-text"),
        }

    except Exception as e:
        logger.exception("Erreur analyse texte")
        return {"error": str(e)}


def _ingredients_to_text(ings: list[dict]) -> str:
    """Formate les ingrédients en lignes propres « - 700 g d'épinards »."""
    out = []
    for i in ings:
        nom = (i.get("nom") or "").strip()
        if not nom:
            continue
        parts = [str(i.get("quantite", "")).strip(), str(i.get("unite", "")).strip(), nom]
        out.append("- " + " ".join(p for p in parts if p))
    return "\n".join(out)


@app.post("/api/enrich/{page_id}")
async def api_enrich_one(page_id: str):
    """Enrichit UNE recette : extrait ses ingrédients (depuis sa page) et les
    écrit dans Notion + le cache."""
    try:
        recette = await notion.get_recipe(page_id)
        if not recette:
            return {"error": "Recette introuvable"}
        d = await llm.extract_ingredients(recette["nom"], recette.get("url", ""))
        ings = d.get("ingredients", [])
        if not ings:
            return {"error": "Aucun ingrédient extrait (pas d'URL exploitable ?)"}
        await db.save_enriched(page_id, recette["nom"], ingredients=json.dumps(ings))
        await notion.update_ingredients(page_id, _ingredients_to_text(ings))
        return {"success": True, "count": len(ings)}
    except Exception as e:
        logger.exception("Erreur enrichissement unitaire")
        return {"error": str(e)}


def _ing_lines(items: list[dict]) -> str:
    """Formate des ingrédients en lignes « - nom : qté unité »."""
    return "\n".join(
        f"- {i['nom']}" + (f" : {i.get('quantite','')} {i.get('unite','')}" if i.get('quantite') else "")
        for i in items
    )


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


@app.post("/api/enrich-all")
async def api_enrich_all():
    """Parcourt toutes les recettes Notion et ajoute les ingrédients manquants."""
    try:
        recettes = await notion.get_all_recipes()
        counts = {"enriched": 0, "skipped": 0, "errors": 0}
        for r in recettes:
            status = await _enrich_one(r)
            counts["errors" if status == "error" else status] += 1
        return {"success": True, "total": len(recettes), **counts}
    except Exception as e:
        logger.exception("Erreur enrichissement masse")
        return {"error": str(e)}


@app.post("/api/enrich-week/{planning_id}")
async def api_enrich_week(planning_id: int):
    """Relance l'enrichissement de toutes les recettes d'un planning (plats +
    accompagnements), sans toucher au reste du catalogue."""
    try:
        planning = await db.get_planning_with_recipes(planning_id)
        if not planning:
            return {"error": "Planning introuvable"}
        plats = json.loads(planning["data_json"]).get("plats", [])

        # Recettes uniques du planning (dédup par notion_id), format attendu par
        # _enrich_one : {id, nom, url}.
        seen: set[str] = set()
        recettes: list[dict] = []
        for p in plats:
            for src in (p, *plat_accompagnements(p)):
                nid = src.get("notion_id")
                if nid and nid not in seen:
                    seen.add(nid)
                    recettes.append({"id": nid, "nom": src.get("nom_recette", ""),
                                     "url": src.get("url", "")})

        counts = {"enriched": 0, "skipped": 0, "errors": 0}
        for r in recettes:
            status = await _enrich_one(r)
            counts["errors" if status == "error" else status] += 1
        return {"success": True, "total": len(recettes), **counts}
    except Exception as e:
        logger.exception("Erreur enrichissement semaine")
        return {"error": str(e)}


@app.get("/api/enrich-all/stream")
async def api_enrich_all_stream():
    """Variante SSE : enrichit toutes les recettes en streamant la progression
    (évite le timeout d'une requête synchrone longue et donne un retour live)."""
    async def gen():
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

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


@app.post("/ajouter-recette")
async def ajouter_recette(
    request: Request,
    url: str = Form(""),
    nom: str = Form(""),
    repas: list[str] = Form([]),
    tags: list[str] = Form([]),
    base: list[str] = Form([]),
    nature: str = Form("Recette"),
    moment: str = Form(""),
    ingredients_manual: str = Form(""),
    steps: list[str] = Form([]),
    image_url: str = Form(""),
):
    """Ajoute une recette depuis une URL ou manuellement."""
    instructions_manual = "\n".join(s.strip() for s in steps if s.strip())
    error = None
    success = None

    try:
        if url and not nom:
            # Extraction automatique via LLM
            extracted = await llm.extract_recipe_from_url(url)
            nom = extracted.get("nom", "")
            if not repas and extracted.get("type_repas"):
                repas = [extracted["type_repas"]]
            if not tags:
                tags = extracted.get("tags", [])
            if not base:
                base = extracted.get("base", [])
        elif not nom:
            error = "Il faut au moins un nom ou une URL."
        else:
            # Si que URL sans extraction
            pass

        if not error:
            result = await notion.create_recipe(
                nom=nom,
                url=url,
                repas=repas,
                tags=tags,
                moment=moment,
                nature=nature,
                base=base,
            )
            page_id = result.get("id", "")
            recette_url = result.get("url", "")
            logger.info(f"Recette ajoutée: {nom} → {recette_url}")

            # Sauvegarder les ingrédients, instructions et image : chaque écriture
            # est isolée pour remonter précisément ce qui n'a pas pu être enregistré
            # (au lieu d'avaler l'échec et d'annoncer un « succès » trompeur).
            echecs: list[str] = []

            async def _etape(label: str, coro) -> None:
                try:
                    await coro
                except Exception as e:
                    logger.warning(f"Impossible de sauvegarder {label}: {e}")
                    echecs.append(label)

            if page_id and ingredients_manual:
                await _etape("les ingrédients", notion.update_ingredients(page_id, ingredients_manual))
            if page_id and instructions_manual:
                await _etape("les instructions", notion.append_instructions(page_id, instructions_manual))
            if page_id and image_url:
                await _etape("l'image", notion.update_image(page_id, image_url))
            if page_id and ingredients_manual:
                # Cache local : ingrédients STRUCTURÉS (pour scaling + nutrition)
                ings_list = [
                    i for i in (parse_ingredient_line(l) for l in ingredients_manual.split("\n"))
                    if i and i["nom"]
                ]
                await _etape("le cache local des ingrédients", db.save_enriched(
                    notion_id=page_id, recipe_name=nom, ingredients=json.dumps(ings_list),
                ))

            if echecs:
                # Échec partiel : la page a bien été créée mais des infos manquent.
                # On n'affiche PAS un « succès » nu.
                error = (f"Recette « {nom} » créée dans Notion, mais impossible d'enregistrer : "
                         + ", ".join(echecs) + ". Ré-essaie depuis la page « Enrichir ».")
            else:
                success = f"Recette « {nom} » ajoutée avec succès !"

    except Exception as e:
        logger.exception("Erreur ajout recette")
        error = f"Erreur : {str(e)}"

    return templates.TemplateResponse(
        "ajouter.html",
        {
            "request": request,
            "success": success,
            "error": error,
            "repas_options": REPAS_OPTIONS,
            "tag_options": TAG_OPTIONS,
            "base_options": BASE_OPTIONS,
            "nature_options": NATURE_OPTIONS,
        },
    )


@app.get("/api/alternatives/{planning_id}")
async def api_alternatives(planning_id: int):
    """Retourne les recettes disponibles non utilisées dans ce planning."""
    try:
        planning = await db.get_planning_with_recipes(planning_id)
        if not planning:
            return {"error": "Planning introuvable"}

        # Recettes déjà dans le planning
        used_names = {r["recipe_name"] for r in planning.get("recipes", [])}

        # Toutes les recettes Notion. On expose la Nature pour que le picker
        # distingue les recettes des « briques » (composants simples). On garde
        # aussi les briques (Nature=Ingrédient) dans la liste : réutilisables en
        # plat (n'importe quelle Base) comme en accompagnement (Base=Légume).
        recettes = await notion.get_all_recipes()
        alternatives = [
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

        return {"alternatives": alternatives}
    except Exception as e:
        logger.exception("Erreur récupération alternatives")
        return {"error": str(e), "alternatives": []}


@app.post("/api/update-meal/{planning_id}")
async def api_update_meal(
    planning_id: int,
    request: Request,
):
    """Remplace un repas dans le planning et regénère la liste de courses."""

    data = await request.json()
    jour = data.get("jour")
    moment = data.get("moment")  # "midi" ou "soir"
    nouvelle_recette = (data.get("nouvelle_recette") or "").strip()

    if jour not in range(1, 8) or moment not in ("midi", "soir") or not nouvelle_recette:
        return {"error": "Paramètres invalides (jour 1-7, moment midi/soir, recette)"}

    try:
        # Récupérer le planning actuel
        planning = await db.get_planning_with_recipes(planning_id)
        if not planning:
            return {"error": "Planning introuvable"}

        planning_data = json.loads(planning["data_json"])
        plats = planning_data["plats"]

        # Chercher et remplacer le plat
        target = next((p for p in plats if p["jour"] == jour and p["moment"] == moment), None)
        if not target:
            return {"error": "Repas non trouvé dans le planning"}
        target["nom_recette"] = nouvelle_recette
        target["notion_id"] = target["url"] = target["notion_url"] = ""
        r = _index_by_name(await notion.get_all_recipes()).get(nouvelle_recette.lower().strip())
        if r:
            _apply_recipe_info(target, r)
        # Le plat principal change : ses accompagnements ne le concernent plus,
        # on repart d'une assiette vide (l'utilisateur les recompose ensuite).
        target["accompagnements"] = []
        target.pop("accompagnement", None)

        # Regénérer la liste de courses + persister le planning (mêmes règles
        # que /generate-shopping : pas d'extraction LLM à l'aveugle, mise à
        # l'échelle par jour, titre de recette par ingrédient).
        return await _refresh_shopping_and_save(planning, planning_id, planning_data, plats)

    except Exception as e:
        logger.exception("Erreur update meal")
        return {"error": str(e)}


@app.post("/api/free-meal/{planning_id}")
async def api_free_meal(planning_id: int, request: Request):
    """Crée une recette minimale « repas libre » (nom + quelques ingrédients)
    puis la place dans un créneau du planning. Utile pour un plat qui n'existe
    pas encore en base (ex. « Steak + haricots verts »).

    JSON attendu : {jour:int, moment:str, nom:str, ingredients:str}
    (ingredients = texte multi-lignes, une ligne par ingrédient).
    """
    data = await request.json()
    jour = data.get("jour")
    moment = data.get("moment")  # "midi" ou "soir"
    nom = (data.get("nom") or "").strip()
    ingredients_text = data.get("ingredients") or ""

    if jour not in range(1, 8) or moment not in ("midi", "soir") or not nom:
        return {"error": "Paramètres invalides (jour 1-7, moment midi/soir, nom)"}

    try:
        # Récupérer le planning et repérer le créneau cible AVANT de créer la
        # recette (évite de créer une page Notion orpheline si le créneau
        # n'existe pas).
        planning = await db.get_planning_with_recipes(planning_id)
        if not planning:
            return {"error": "Planning introuvable"}

        planning_data = json.loads(planning["data_json"])
        plats = planning_data["plats"]
        target = next((p for p in plats if p["jour"] == jour and p["moment"] == moment), None)
        if not target:
            return {"error": "Repas non trouvé dans le planning"}

        # Parser les ingrédients saisis (une ligne → {nom, quantite, unite}).
        structured = [
            i for i in (parse_ingredient_line(l) for l in ingredients_text.split("\n"))
            if i and i["nom"]
        ]

        # Créer la VRAIE recette minimale réutilisable dans Notion.
        result = await notion.create_recipe(nom=nom, repas="Plat")
        page_id = result.get("id", "")
        if not page_id:
            return {"error": "Création de la recette impossible"}

        # Écrire les ingrédients : Notion + cache local structuré (pour scaling,
        # nutrition et reconstruction de la liste de courses).
        if structured:
            await notion.update_ingredients(page_id, _ingredients_to_text(structured))
            await db.save_enriched(page_id, nom, ingredients=json.dumps(structured))

        # Placer la recette fraîchement créée dans le créneau : on construit le
        # dict recette nous-mêmes (on a déjà son id/url via create_recipe) plutôt
        # que de re-fetch tout le catalogue.
        target["nom_recette"] = nom
        target["notion_id"] = target["url"] = target["notion_url"] = ""
        _apply_recipe_info(target, {
            "id": page_id,
            "url": "",                        # pas d'URL source pour un repas libre
            "notion_url": result.get("url", ""),
            "repas": ["Plat"],
            "tags": [],
        })
        # Nouveau plat principal : on réinitialise les accompagnements.
        target["accompagnements"] = []
        target.pop("accompagnement", None)

        # Régénérer la liste de courses + persister EXACTEMENT comme /api/update-meal.
        return await _refresh_shopping_and_save(planning, planning_id, planning_data, plats)

    except Exception as e:
        logger.exception("Erreur repas libre")
        return {"error": str(e)}


async def _resolve_side(nom: str) -> dict:
    """Construit le dict accompagnement pour un nom de recette, en reliant à
    Notion si la recette y existe (sinon accompagnement « libre » sans id)."""
    acc = {"nom_recette": nom, "notion_id": "", "url": "", "notion_url": ""}
    r = _index_by_name(await notion.get_all_recipes()).get(nom.lower().strip())
    if r:
        acc.update(notion_id=r["id"], url=r.get("url", ""), notion_url=r.get("notion_url", ""))
    return acc


async def _mutate_sides(planning_id: int, jour, moment, mutate):
    """Charge le planning, applique `mutate(cible)` sur la liste d'accompagnements
    du repas ciblé, régénère la liste de courses et persiste. Renvoie la réponse
    standard des endpoints planning (success/liste_courses/plats)."""
    if jour not in range(1, 8) or moment not in ("midi", "soir"):
        return {"error": "Paramètres invalides (jour 1-7, moment midi/soir)"}
    try:
        planning = await db.get_planning_with_recipes(planning_id)
        if not planning:
            return {"error": "Planning introuvable"}

        planning_data = json.loads(planning["data_json"])
        plats = planning_data["plats"]
        cible = next((p for p in plats if p["jour"] == jour and p["moment"] == moment), None)
        if not cible:
            return {"error": "Repas non trouvé dans le planning"}

        accs = plat_accompagnements(cible)
        accs = await mutate(accs)
        cible["accompagnements"] = accs
        cible.pop("accompagnement", None)  # migre l'ancien champ singulier

        return await _refresh_shopping_and_save(planning, planning_id, planning_data, plats)
    except Exception as e:
        logger.exception("Erreur mutation accompagnements")
        return {"error": str(e)}


@app.post("/api/add-side/{planning_id}")
async def api_add_side(planning_id: int, request: Request):
    """Ajoute (APPEND) un accompagnement au repas ciblé. Doublon (même nom)
    ignoré. JSON attendu : {jour:int, moment:str, nom:str}."""
    data = await request.json()
    nom = (data.get("nom") or data.get("nouvelle_recette") or "").strip()
    if not nom:
        return {"error": "Nom d'accompagnement manquant"}

    async def mutate(accs: list[dict]) -> list[dict]:
        if any(a.get("nom_recette", "").lower().strip() == nom.lower().strip() for a in accs):
            return accs
        accs.append(await _resolve_side(nom))
        return accs

    return await _mutate_sides(planning_id, data.get("jour"), data.get("moment"), mutate)


@app.post("/api/remove-side/{planning_id}")
async def api_remove_side(planning_id: int, request: Request):
    """Retire UN accompagnement du repas ciblé (par nom).
    JSON attendu : {jour:int, moment:str, nom:str}."""
    data = await request.json()
    nom = (data.get("nom") or "").strip().lower()
    if not nom:
        return {"error": "Nom d'accompagnement manquant"}

    async def mutate(accs: list[dict]) -> list[dict]:
        return [a for a in accs if a.get("nom_recette", "").lower().strip() != nom]

    return await _mutate_sides(planning_id, data.get("jour"), data.get("moment"), mutate)


@app.post("/api/update-side/{planning_id}")
async def api_update_side(planning_id: int, request: Request):
    """Remplace TOUTE la liste d'accompagnements d'un repas par un seul (ou la
    vide si nouvelle_recette est vide). Le modèle est désormais une liste ;
    préférer /api/add-side et /api/remove-side pour composer plusieurs sides."""
    data = await request.json()
    nouvelle_recette = (data.get("nouvelle_recette") or "").strip()

    async def mutate(_accs: list[dict]) -> list[dict]:
        if not nouvelle_recette:
            return []
        return [await _resolve_side(nouvelle_recette)]

    return await _mutate_sides(planning_id, data.get("jour"), data.get("moment"), mutate)


@app.post("/api/brique/{planning_id}")
async def api_brique(planning_id: int, request: Request):
    """Crée une « brique » (composant simple : steak, œuf, riz…) et la place
    dans un créneau du planning, en plat OU en accompagnement.

    Une brique = recette de Nature « Ingrédient » + une/des Base(s), dont
    l'ingrédient de courses est elle-même (son propre nom). Exclue de la
    génération auto mais réutilisable manuellement.

    JSON attendu : {jour:int, moment:str, slot:"plat"|"accompagnement",
    nom:str, base:list[str], quantite:str, unite:str}.
    """
    data = await request.json()
    jour = data.get("jour")
    moment = data.get("moment")  # "midi" ou "soir"
    slot = data.get("slot")      # "plat" ou "accompagnement"
    nom = (data.get("nom") or "").strip()
    base = data.get("base") or []
    if isinstance(base, str):
        base = [base]
    base = [b for b in base if b]
    quantite = (str(data.get("quantite") or "")).strip()
    unite = (str(data.get("unite") or "")).strip()

    if jour not in range(1, 8) or moment not in ("midi", "soir"):
        return {"error": "Paramètres invalides (jour 1-7, moment midi/soir)"}
    if slot not in ("plat", "accompagnement") or not nom:
        return {"error": "Paramètres invalides (slot plat/accompagnement, nom)"}

    try:
        # Repérer le créneau cible AVANT de créer la recette (évite une page
        # Notion orpheline si le créneau n'existe pas).
        planning = await db.get_planning_with_recipes(planning_id)
        if not planning:
            return {"error": "Planning introuvable"}

        planning_data = json.loads(planning["data_json"])
        plats = planning_data["plats"]
        target = next((p for p in plats if p["jour"] == jour and p["moment"] == moment), None)
        if not target:
            return {"error": "Repas non trouvé dans le planning"}

        # Créer la recette brique (Nature=Ingrédient + Base, sans repas forcé).
        result = await notion.create_recipe(nom=nom, nature="Ingrédient", base=base)
        page_id = result.get("id", "")
        if not page_id:
            return {"error": "Création de la brique impossible"}

        # L'ingrédient de courses de la brique est elle-même. On normalise via
        # parse_ingredient_line pour découper qté/unité proprement, avec repli
        # sur les valeurs saisies si le parsing ne renvoie rien d'exploitable.
        parsed = parse_ingredient_line(" ".join(p for p in (quantite, unite, nom) if p))
        if parsed and parsed.get("nom"):
            structured = [parsed]
        else:
            structured = [{"nom": nom, "quantite": quantite, "unite": unite}]

        await notion.update_ingredients(page_id, _ingredients_to_text(structured))
        await db.save_enriched(page_id, nom, ingredients=json.dumps(structured))

        recette = {
            "id": page_id,
            "url": "",
            "notion_url": result.get("url", ""),
            "nature": "Ingrédient",
            "base": base,
            "repas": [],
            "tags": [],
        }

        if slot == "plat":
            # Même logique que /api/free-meal : la brique devient le plat.
            target["nom_recette"] = nom
            target["notion_id"] = target["url"] = target["notion_url"] = ""
            _apply_recipe_info(target, recette)
            target["accompagnements"] = []
            target.pop("accompagnement", None)
        else:
            # La brique s'AJOUTE aux accompagnements (append, pas d'écrasement).
            accs = plat_accompagnements(target)
            accs.append({
                "nom_recette": nom,
                "notion_id": page_id,
                "url": "",
                "notion_url": result.get("url", ""),
                "nature": "Ingrédient",
                "base": base,
            })
            target["accompagnements"] = accs
            target.pop("accompagnement", None)

        return await _refresh_shopping_and_save(planning, planning_id, planning_data, plats)

    except Exception as e:
        logger.exception("Erreur brique")
        return {"error": str(e)}


@app.get("/sw.js")
async def service_worker():
    """Sert le service worker depuis la racine (scope = tout le site)."""
    return FileResponse(
        str(BASE_DIR / "static" / "sw.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/health")
async def health():
    return {"status": "ok", "app": settings.app_title}
