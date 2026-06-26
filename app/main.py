"""App FastAPI — Menu Planner avec génération IA."""

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import Database
from app.llm_client import LLMClient
from app.notion_client import NotionClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_title, version="1.0.0")
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)

# Static files & templates
BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Instances
db = Database()
notion = NotionClient()
llm = LLMClient()

REPAS_OPTIONS = [
    "Plat",
    "Dessert",
    "Entrée",
    "Goûter",
    "Accompagnement",
    "Apéro",
    "Boisson",
    "Petit dej",
    "Légume",
]
TAG_OPTIONS = [
    "Viande",
    "Poisson",
    "Légumes",
    "Soupe",
    "Salade",
    "Diet",
    "Fun",
    "Quiche/tarte",
    "Tartines",
    "Invit��s",
    "Sur le pouce",
    "Végétarien proténiné",
    "1 personne",
]


@app.on_event("startup")
async def startup():
    await db.init()
    logger.info("Base SQLite initialisée")


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

    return templates.TemplateResponse(
        "planning.html",
        {
            "request": request,
            "planning": planning,
            "plats": data.get("plats", []),
            "liste_courses": data.get("liste_courses", []),
            "repas_options": REPAS_OPTIONS,
        },
    )


@app.get("/recettes", response_class=HTMLResponse)
async def liste_recettes(request: Request):
    """Liste toutes les recettes de la base Notion."""
    try:
        recettes = await notion.get_all_recipes()
    except Exception as e:
        logger.error(f"Erreur Notion: {e}")
        recettes = []

    # Stats
    total = len(recettes)
    par_type: dict[str, int] = {}
    for r in recettes:
        t = r["repas"] or "Non classé"
        par_type[t] = par_type.get(t, 0) + 1

    return templates.TemplateResponse(
        "recettes.html",
        {
            "request": request,
            "recettes": recettes,
            "total": total,
            "par_type": sorted(par_type.items()),
            "repas_options": REPAS_OPTIONS,
            "tag_options": TAG_OPTIONS,
        },
    )


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
    saison: str = Form(...),
    temperature: str = Form(""),
    nb_personnes: int = Form(4),
    ingredients_force: str = Form(""),
):
    """Génère un planning via le LLM et sauvegarde."""
    try:
        # 1. Récupérer toutes les recettes Notion
        recettes = await notion.get_all_recipes()
        recettes = [
            r for r in recettes if r["repas"] in ("Plat", "Entrée", "Accompagnement", "Légume", "")
        ]

        if not recettes:
            return templates.TemplateResponse(
                "index.html",
                {
                    "request": request,
                    "error": "Aucune recette trouvée dans Notion. Ajoutez-en d'abord !",
                    "repas_options": REPAS_OPTIONS,
                    "tag_options": TAG_OPTIONS,
                },
            )

        # 2. Récupérer les recettes récemment utilisées (éviter répétitions)
        exclues = await db.get_recent_recipe_names(weeks=4)
        logger.info(f"{len(recettes)} recettes dispo, {len(exclues)} exclues")

        # 3. Générer le planning via Ollama
        plats = await llm.generate_planning(
            recettes=recettes,
            saison=saison,
            temperature=temperature,
            nb_personnes=nb_personnes,
            ingredients_force=ingredients_force,
            recettes_exclues=list(exclues),
        )

        # 4. Pour chaque plat sélectionné, extraire les ingrédients
        liste_courses: list[dict] = []
        courses_map: dict[str, dict] = {}  # dédoublonnage

        for plat in plats:
            plat["notion_id"] = ""
            # Chercher dans la base Notion
            for r in recettes:
                if r["nom"].lower().strip() == plat["nom_recette"].lower().strip():
                    plat["notion_id"] = r["id"]
                    plat["url"] = r.get("url", "")
                    break

            # Extraire les ingrédients
            try:
                ingredients_data = await llm.extract_ingredients(
                    plat["nom_recette"],
                    plat.get("url", ""),
                    nb_personnes,
                )
                for ing in ingredients_data.get("ingredients", []):
                    nom_ing = ing["nom"].lower().strip()
                    if nom_ing in courses_map:
                        # Additionner les quantités si même unité
                        existing = courses_map[nom_ing]
                        if existing["unite"] == ing.get("unite", ""):
                            try:
                                q1 = float(str(existing["quantite"]).replace(",", "."))
                                q2 = float(str(ing["quantite"]).replace(",", "."))
                                existing["quantite"] = str(q1 + q2)
                            except (ValueError, TypeError):
                                pass
                    else:
                        courses_map[nom_ing] = {
                            "nom": ing["nom"],
                            "quantite": ing.get("quantite", ""),
                            "unite": ing.get("unite", ""),
                        }
            except Exception as e:
                logger.warning(f"Erreur extraction ingrédients pour {plat['nom_recette']}: {e}")

        liste_courses = sorted(courses_map.values(), key=lambda x: x["nom"])

        # 5. Ajouter les ingrédients forcés à la liste de courses si pas déjà présents
        if ingredients_force:
            forces = [i.strip() for i in ingredients_force.split(",")]
            for f in forces:
                if f.lower() not in courses_map:
                    liste_courses.append({"nom": f, "quantite": "", "unite": ""})

        # 6. Sauvegarder en base
        data = {
            "plats": plats,
            "liste_courses": liste_courses,
        }
        recipes_to_save = [
            {
                "notion_id": p.get("notion_id", ""),
                "recipe_name": p["nom_recette"],
                "repas_type": p.get("type_repas", ""),
                "jour": p["jour"],
                "moment": p["moment"],
            }
            for p in plats
        ]

        planning_id = await db.save_planning(
            week_start=week_start,
            saison=saison,
            nb_personnes=nb_personnes,
            ingredients_force=ingredients_force,
            data_json=json.dumps(data, ensure_ascii=False),
            recipes=recipes_to_save,
        )

        return RedirectResponse(url=f"/planning/{planning_id}", status_code=303)

    except Exception as e:
        logger.exception("Erreur lors de la génération")
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "error": f"Erreur : {str(e)}",
                "repas_options": REPAS_OPTIONS,
                "tag_options": TAG_OPTIONS,
            },
        )


@app.post("/ajouter-recette")
async def ajouter_recette(
    request: Request,
    url: str = Form(""),
    nom: str = Form(""),
    repas: str = Form(""),
    tags: list[str] = Form([]),
):
    """Ajoute une recette depuis une URL ou manuellement."""
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
            )
            recette_url = result.get("url", "")
            success = f"Recette « {nom} » ajoutée avec succès !"
            logger.info(f"Recette ajoutée: {nom} → {recette_url}")

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


@app.get("/health")
async def health():
    return {"status": "ok", "app": settings.app_title}
