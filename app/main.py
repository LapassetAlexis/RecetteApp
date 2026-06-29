"""App FastAPI — Menu Planner avec génération IA."""

import base64
import binascii
import json
import logging
import random
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import REPAS_OPTIONS, TAG_OPTIONS, settings
from app.database import Database
from app.llm_client import LLMClient
from app.notion_client import NotionClient
from app.nutrition import estimate_nutrition
from app.text_utils import clean_recipe_title, merge_ingredients, parse_ingredient_line, split_instructions

VERSION = "1.1.0"

# Nombre max de recettes envoyées au LLM pour la génération du planning.
# Plus haut = plus de variété ; reste léger en tokens grâce au format compact.
# Surtout utile en cloud (Gemini/Groq) ; en Ollama local un petit modèle peut
# ralentir avec une très longue liste.
MAX_RECIPES_FOR_LLM = 100

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
    _PUBLIC_PREFIXES = ("/health", "/static")

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

# ── Pages ──────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Page d'accueil : formulaire de génération."""
    today = date.today()
    # Calcul du lundi de la semaine
    lundi = today - timedelta(days=today.weekday())
    week_start = lundi.isoformat()

    # Saison automatique
    mois = today.month
    if 3 <= mois <= 5:
        saison_default = "Printemps"
    elif 6 <= mois <= 8:
        saison_default = "Été"
    elif 9 <= mois <= 11:
        saison_default = "Automne"
    else:
        saison_default = "Hiver"

    # Dernier planning
    dernier = await db.get_last_planning()
    planning_id = dernier["id"] if dernier else None

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "week_start": week_start,
            "saison_default": saison_default,
            "nb_personnes": 4,
            "planning_id": planning_id,
            "repas_options": REPAS_OPTIONS,
            "tag_options": TAG_OPTIONS,
        },
    )


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

    return templates.TemplateResponse(
        "planning.html",
        {
            "request": request,
            "planning": planning,
            "plats": data.get("plats", []),
            "liste_courses": data.get("liste_courses", []),
            "per_day": per_day,
            "valide": bool(planning.get("valide", 0)),
            "repas_options": REPAS_OPTIONS,
        },
    )


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

    # Stats pour les filtres (type / état / tag)
    total = len(recettes)
    par_type: dict[str, int] = {}
    par_etat: dict[str, int] = {}
    par_tag: dict[str, int] = {}
    for r in recettes:
        par_type[r["repas"] or "Non classé"] = par_type.get(r["repas"] or "Non classé", 0) + 1
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
            "par_type": sorted(par_type.items()),
            "par_etat": sorted(par_etat.items()),
            "par_tag": sorted(par_tag.items(), key=lambda kv: -kv[1]),  # tags par fréquence
            "repas_options": REPAS_OPTIONS,
            "tag_options": TAG_OPTIONS,
        },
    )


BASE_SERVINGS = 4  # base d'extraction des ingrédients (cf. extract_ingredients)


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
                nutrition = json.loads(cached["nutrition"]) or None
                if nutrition:
                    nutrition.setdefault("source", "source")
            except (json.JSONDecodeError, TypeError):
                nutrition = None
        if not nutrition and ingredients:
            nutrition = estimate_nutrition(ingredients, BASE_SERVINGS)

        return templates.TemplateResponse(
            "recette_detail.html",
            {
                "request": request,
                "recette": recette,
                "ingredients": ingredients,
                "base_servings": BASE_SERVINGS,
                "instructions": instructions,
                "nutrition": nutrition,
            },
        )
    except Exception:
        logger.exception("Erreur détail recette")
        return HTMLResponse("Erreur interne lors du chargement de la recette.", status_code=500)


@app.get("/recette/{page_id}/enrichir", response_class=HTMLResponse)
async def enrichir_page(request: Request, page_id: str):
    """Étape 1 : re-fetch la source et pré-remplit un formulaire éditable
    (ingrédients structurés + instructions nettoyées) à valider."""
    recette = await notion.get_recipe(page_id)
    if not recette:
        return HTMLResponse("Recette non trouvée", status_code=404)

    ingredients: list[dict] = []
    instructions_text = ""
    image_url = ""
    nutrition_src: dict = {}

    # « Enrichir » = re-fetch la SOURCE en priorité (données d'origine), pas le
    # cache qui peut contenir d'anciennes extractions inexactes.
    if recette.get("url"):
        try:
            info = await llm.extract_recipe_from_url(recette["url"])
            ingredients = [i for i in (parse_ingredient_line(x) for x in info.get("ingredients", [])) if i and i["nom"]]
            instructions_text = info.get("instructions", "")
            image_url = info.get("image_url", "")
            nutrition_src = info.get("nutrition") or {}
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
            "repas_options": REPAS_OPTIONS,
            "tag_options": TAG_OPTIONS,
        },
    )


@app.post("/recette/{page_id}/enrichir")
async def enrichir_submit(
    request: Request,
    page_id: str,
    repas: str = Form(""),
    tags: list[str] = Form([]),
    ingredients_text: str = Form(""),
    steps: list[str] = Form([]),
    image_url: str = Form(""),
    nutrition_json: str = Form(""),
):
    """Étape 2 : applique les changements validés à la recette Notion + cache."""
    recette = await notion.get_recipe(page_id)
    if not recette:
        return RedirectResponse(url="/recettes", status_code=303)

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
    try:
        if structured:
            await db.save_enriched(page_id, recette["nom"], ingredients=json.dumps(structured), nutrition=nutrition)
            await notion.update_ingredients(page_id, _ingredients_to_text(structured))
        if instructions_text:
            await notion.rewrite_recipe_body(page_id, instructions_text)
        if image_url:
            await notion.update_image(page_id, image_url)
        await notion.update_recipe_meta(page_id, repas=repas, tags=tags or None)
    except Exception:
        logger.exception("Erreur validation enrichissement")

    return RedirectResponse(url=f"/recette/{page_id}", status_code=303)


@app.get("/ajouter", response_class=HTMLResponse)
async def ajouter_page(request: Request):
    """Page pour ajouter une recette depuis une URL."""
    return templates.TemplateResponse(
        "ajouter.html",
        {
            "request": request,
            "repas_options": REPAS_OPTIONS,
            "tag_options": TAG_OPTIONS,
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


@app.post("/generer")
async def generer(
    request: Request,
    week_start: str = Form(...),
    nb_lun: int = Form(2),
    nb_mar: int = Form(2),
    nb_mer: int = Form(2),
    nb_jeu: int = Form(2),
    nb_ven: int = Form(2),
    nb_sam: int = Form(4),
    nb_dim: int = Form(4),
    saison: str = Form(...),
    temperature: str = Form(""),
    ingredients_force: str = Form(""),
    tags: list[str] = Form([]),
    etat: str = Form(""),
    custom_prompt: str = Form(""),
    midi_groups: str = Form("1,1,2,2,2,3,4"),
):
    """Génère un planning via le LLM et sauvegarde."""
    # Calculer le nb de personnes moyen pour les ingrédients
    pers = [nb_lun, nb_mar, nb_mer, nb_jeu, nb_ven, nb_sam, nb_dim]
    nb_personnes = max(pers)  # On prend le max pour les quantités
    per_day = ",".join(str(p) for p in pers)

    try:
        day_groups = [int(x) for x in midi_groups.split(",")]
    except (ValueError, TypeError):
        day_groups = [1, 1, 2, 2, 2, 3, 4]

    def _error_ctx(message: str) -> dict:
        """Contexte de re-rendu du formulaire en préservant toute la saisie."""
        return {
            "request": request,
            "error": message,
            "repas_options": REPAS_OPTIONS,
            "tag_options": TAG_OPTIONS,
            "week_start": week_start,
            "saison_default": saison,
            "temperature_default": temperature,
            "day_groups": day_groups,
            "day_pers": pers,
            "midi_groups_value": midi_groups,
            "ingredients_force": ingredients_force,
            "custom_prompt": custom_prompt,
        }

    try:
        # 1. Récupérer toutes les recettes Notion
        recettes = await notion.get_all_recipes()
        recettes = [
            r for r in recettes if r["repas"] in ("Plat", "Entrée", "Accompagnement", "Légume", "")
        ]

        if not recettes:
            return templates.TemplateResponse(
                "index.html",
                _error_ctx("Aucune recette trouvée dans Notion. Ajoutez-en d'abord !"),
            )

        # 2. Filtrer par tags si sélectionnés
        if tags and tags[0]:  # tags non vide
            recettes = [r for r in recettes if any(t in r["tags"] for t in tags)]

        # 3. Filtrer par état si sélectionné
        if etat:
            recettes = [r for r in recettes if r["etat"] == etat]

        # 4. Récupérer les recettes récemment utilisées (éviter répétitions)
        exclues = await db.get_recent_recipe_names(weeks=4)
        logger.info(f"{len(recettes)} recettes dispo, {len(exclues)} exclues")

        # Filtrer les exclues
        recettes_filtered = [r for r in recettes if r["nom"] not in exclues]
        # On envoie le plus de recettes possible pour maximiser la variété du
        # menu. Grâce au format compact (~15 tokens/recette), MAX_RECIPES_FOR_LLM
        # tient largement dans le contexte de Gemini/Groq. On n'échantillonne
        # au hasard que si la base dépasse ce plafond.
        if len(recettes_filtered) > MAX_RECIPES_FOR_LLM:
            recettes_sample = random.sample(recettes_filtered, MAX_RECIPES_FOR_LLM)
        else:
            recettes_sample = recettes_filtered
        logger.info(f"Envoi de {len(recettes_sample)} recettes au LLM (sur {len(recettes_filtered)} dispo)")

        # 3. Générer le planning via Ollama
        plats = await llm.generate_planning(
            recettes=recettes_sample,
            saison=saison,
            temperature=temperature,
            nb_personnes=nb_personnes,
            ingredients_force=ingredients_force,
            recettes_exclues=list(exclues),
            custom_prompt=custom_prompt,
            midi_groups=midi_groups,
            per_day=per_day,
        )

        # 4. Associer chaque plat aux infos Notion
        for plat in plats:
            plat["notion_id"] = ""
            for r in recettes:
                if r["nom"].lower().strip() == plat["nom_recette"].lower().strip():
                    plat["notion_id"] = r["id"]
                    plat["url"] = r.get("url", "")
                    plat["notion_url"] = r.get("notion_url", "")
                    plat["repas"] = r.get("repas", "")
                    plat["tags"] = r.get("tags", [])
                    break

        # 5. Sauvegarder en BROUILLON (valide=0). Le planning n'apparaît dans
        #    l'historique qu'après validation explicite par l'utilisateur.
        #    On purge d'abord l'éventuel brouillon précédent non validé.
        await db.delete_draft_plannings()
        data = {
            "plats": plats,
            "liste_courses": [],
            "per_day": per_day,
        }
        planning_id = await db.save_planning(
            week_start=week_start,
            saison=saison,
            nb_personnes=nb_personnes,
            ingredients_force=ingredients_force,
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
        logger.exception("Erreur lors de la génération")
        return templates.TemplateResponse(
            "index.html",
            _error_ctx(f"Erreur : {str(e)}"),
        )


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

        liste_courses = []
        try:
            liste_courses = await llm.batch_extract_ingredients(plats, planning["nb_personnes"])
        except Exception as e:
            logger.warning(f"Batch échoué ({e}), extraction individuelle...")
            collected = []
            for plat in plats:
                try:
                    d = await llm.extract_ingredients(plat["nom_recette"], plat.get("url", ""), planning["nb_personnes"])
                    collected.extend(d.get("ingredients", []))
                except Exception:
                    pass
            liste_courses = collected
        # Normalisation/fusion déterministe (dédup + addition par nom+unité),
        # y compris pour nettoyer d'éventuels doublons de la sortie du batch LLM.
        liste_courses = merge_ingredients(liste_courses)

        # Ajouter les ingrédients forcés
        force = planning.get("ingredients_force", "")
        if force:
            for f in [i.strip() for i in force.split(",") if i.strip()]:
                if f.lower() not in {i["nom"].lower() for i in liste_courses}:
                    liste_courses.append({"nom": f, "quantite": "", "unite": ""})

        # Sauvegarder la liste de courses dans le planning.
        # NB : on n'écrit PAS cette liste dans Notion par recette — c'est une
        # liste agrégée/dédupliquée de la semaine, pas les ingrédients d'une
        # recette donnée (ceux-ci sont gérés à l'ajout / via enrich-all).
        planning_data["liste_courses"] = liste_courses
        await db.update_planning_data(planning_id, json.dumps(planning_data, ensure_ascii=False))

        return {"success": True, "liste_courses": liste_courses}

    except Exception as e:
        logger.exception("Erreur génération liste courses")
        return {"error": str(e)}


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
        return {"success": True}
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
            "ingredients": ingredients,
            "instructions": info.get("instructions", ""),
            "image_url": info.get("image_url", ""),
            "moment": "Les deux",
            "source": info.get("source", ""),
        }

    except Exception as e:
        logger.exception("Erreur analyse URL")
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


@app.post("/api/enrich-all")
async def api_enrich_all():
    """Parcourt toutes les recettes Notion et ajoute les ingrédients manquants."""
    try:
        recettes = await notion.get_all_recipes()
        total = len(recettes)
        enriched = 0
        skipped = 0
        errors = 0

        for r in recettes:
            nid = r["id"]
            nom = r["nom"]
            if not nid or not nom:
                skipped += 1
                continue

            # Chercher dans le cache local
            cached = await db.get_enriched(nid)
            ingredients_txt = ""

            if cached and cached.get("ingredients"):
                try:
                    ing_list = json.loads(cached["ingredients"])
                    if ing_list:
                        ingredients_txt = "\n".join(
                            f"- {i['nom']}" + (f" : {i.get('quantite','')} {i.get('unite','')}" if i.get('quantite') else "")
                            for i in ing_list
                        )
                except Exception: pass

            # Sinon, extraire via LLM
            if not ingredients_txt and r.get("url"):
                try:
                    d = await llm.extract_ingredients(nom, r.get("url", ""))
                    ings = d.get("ingredients", [])
                    if ings:
                        ingredients_txt = "\n".join(
                            f"- {i['nom']}" + (f" : {i.get('quantite','')} {i.get('unite','')}" if i.get('quantite') else "")
                            for i in ings
                        )
                        # Mettre en cache
                        await db.save_enriched(nid, nom, ingredients=json.dumps(ings))
                except Exception: pass

            if ingredients_txt:
                try:
                    await notion.update_ingredients(nid, ingredients_txt)
                    enriched += 1
                except Exception as e:
                    logger.warning(f"Erreur écriture pour {nom}: {e}")
                    errors += 1
            else:
                skipped += 1

        return {
            "success": True,
            "total": total,
            "enriched": enriched,
            "skipped": skipped,
            "errors": errors,
        }

    except Exception as e:
        logger.exception("Erreur enrichissement masse")
        return {"error": str(e)}


@app.post("/ajouter-recette")
async def ajouter_recette(
    request: Request,
    url: str = Form(""),
    nom: str = Form(""),
    repas: str = Form(""),
    tags: list[str] = Form([]),
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
            if not repas:
                repas = extracted.get("type_repas", "")
            if not tags:
                tags = extracted.get("tags", [])
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
            )
            page_id = result.get("id", "")
            recette_url = result.get("url", "")
            success = f"Recette « {nom} » ajoutée avec succès !"
            logger.info(f"Recette ajoutée: {nom} → {recette_url}")

            # Sauvegarder les ingrédients, instructions et image
            if page_id and (ingredients_manual or instructions_manual or image_url):
                try:
                    if ingredients_manual:
                        await notion.update_ingredients(page_id, ingredients_manual)
                    if instructions_manual:
                        await notion.append_instructions(page_id, instructions_manual)
                    if image_url:
                        await notion.update_image(page_id, image_url)
                    # Cache local : ingrédients STRUCTURÉS (pour scaling + nutrition)
                    if ingredients_manual:
                        ings_list = [
                            i for i in (parse_ingredient_line(l) for l in ingredients_manual.split("\n"))
                            if i and i["nom"]
                        ]
                        await db.save_enriched(
                            notion_id=page_id,
                            recipe_name=nom,
                            ingredients=json.dumps(ings_list),
                        )
                except Exception as e:
                    logger.warning(f"Impossible de sauvegarder les infos: {e}")

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

        # Toutes les recettes Notion
        recettes = await notion.get_all_recipes()
        alternatives = [
            {"nom": r["nom"], "repas": r["repas"], "tags": r["tags"]}
            for r in recettes
            if r["nom"] not in used_names and r["repas"] in ("Plat", "Entrée", "Accompagnement", "Légume", "")
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
    nouvelle_recette = data.get("nouvelle_recette", "")

    if not all([jour, moment, nouvelle_recette]):
        return {"error": "Paramètres manquants (jour, moment, nouvelle_recette)"}

    try:
        # Récupérer le planning actuel
        planning = await db.get_planning_with_recipes(planning_id)
        if not planning:
            return {"error": "Planning introuvable"}

        planning_data = json.loads(planning["data_json"])
        plats = planning_data["plats"]

        # Chercher et remplacer le plat
        trouve = False
        for plat in plats:
            if plat["jour"] == jour and plat["moment"] == moment:
                plat["nom_recette"] = nouvelle_recette
                plat["notion_id"] = ""
                plat["url"] = ""
                plat["notion_url"] = ""
                # Chercher les infos dans Notion
                recettes = await notion.get_all_recipes()
                for r in recettes:
                    if r["nom"].lower().strip() == nouvelle_recette.lower().strip():
                        plat["notion_id"] = r["id"]
                        plat["url"] = r.get("url", "")
                        plat["notion_url"] = r.get("notion_url", "")
                        plat["repas"] = r.get("repas", "")
                        plat["tags"] = r.get("tags", [])
                        break
                trouve = True
                break

        if not trouve:
            return {"error": "Repas non trouvé dans le planning"}

        # Regénérer la liste de courses. On privilégie le cache local pour
        # chaque plat ; on n'appelle le LLM que pour les recettes sans cache
        # (typiquement uniquement le plat qui vient d'être remplacé).
        nb_personnes = planning.get("nb_personnes", 4)
        collected: list[dict] = []

        for plat in plats:
            nid = plat.get("notion_id", "")
            cached = await db.get_enriched(nid) if nid else None
            if cached and cached.get("ingredients"):
                try:
                    collected.extend(json.loads(cached["ingredients"]))
                    continue
                except (json.JSONDecodeError, TypeError) as e:
                    logger.debug(f"Cache ingrédients illisible pour {nid}: {e}")
            # Pas de cache → extraction LLM, puis mise en cache
            try:
                ingredients_data = await llm.extract_ingredients(
                    plat["nom_recette"], plat.get("url", ""), nb_personnes,
                )
                ings = ingredients_data.get("ingredients", [])
                collected.extend(ings)
                if nid and ings:
                    await db.save_enriched(nid, plat["nom_recette"], ingredients=json.dumps(ings))
            except Exception as e:
                logger.warning(f"Extraction ingrédients échouée pour {plat['nom_recette']}: {e}")

        # Fusion déterministe (dédup + addition par nom+unité)
        liste_courses = merge_ingredients(collected)
        planning_data["liste_courses"] = liste_courses

        # Mettre à jour le planning existant (ne pas en créer un nouveau)
        await db.update_planning(
            planning_id=planning_id,
            data_json=json.dumps(planning_data, ensure_ascii=False),
            recipes=[{"notion_id": p.get("notion_id", ""), "recipe_name": p["nom_recette"], "repas_type": p.get("type_repas", ""), "jour": p["jour"], "moment": p["moment"]} for p in plats],
        )

        return {"success": True, "liste_courses": liste_courses, "plats": plats}

    except Exception as e:
        logger.exception("Erreur update meal")
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
