"""App FastAPI — Menu Planner avec génération IA."""

import asyncio
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
from app.cooklang import parse as parse_cook, to_html as cook_to_html
from app.database import Database
from app.llm_client import LLMClient
from app.notion_client import NotionClient

VERSION = "1.1.0"

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
# Version dans tous les templates
templates.env.globals["version"] = VERSION

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
    try:
        await notion.ensure_ingredients_field()
    except Exception as e:
        logger.warning(f"Impossible de créer le champ Ingrédients Notion: {e}")


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


@app.get("/recette/{page_id}", response_class=HTMLResponse)
async def detail_recette(request: Request, page_id: str):
    """Page détail d'une recette avec rendu Cooklang."""
    try:
        recettes = await notion.get_all_recipes()
        recette = next((r for r in recettes if r["id"] == page_id), None)
        if not recette:
            return HTMLResponse("Recette non trouvée", status_code=404)

        # Chercher les ingrédients dans le cache
        cached = await db.get_enriched(page_id)
        cook_content = f">> Serves: 4\n>> Source: {recette.get('url', '')}\n\n"
        if cached and cached.get("ingredients"):
            import json
            try:
                ings = json.loads(cached["ingredients"])
                for i in ings:
                    q = i.get("quantite", "")
                    u = i.get("unite", "")
                    if q and u:
                        cook_content += f"@{i['nom']}{{{q}%{u}}}\n"
                    elif q:
                        cook_content += f"@{i['nom']}{{{q}}}\n"
                    else:
                        cook_content += f"@{i['nom']}\n"
            except: pass
        cook_content += "\n"

        recipe_obj = parse_cook(cook_content)
        html_content = cook_to_html(recipe_obj)

        return templates.TemplateResponse(
            "recette_detail.html",
            {
                "request": request,
                "recette": recette,
                "cook_html": html_content,
                "cook_raw": cook_content,
            },
        )
    except Exception as e:
        logger.exception("Erreur détail recette")
        return HTMLResponse(f"Erreur: {e}", status_code=500)


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
                    "custom_prompt": custom_prompt,
                },
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
        import random
        random.seed()
        recettes_filtered = [r for r in recettes if r["nom"] not in exclues]
        # Limiter à 30 max pour éviter le 413 Payload Too Large (Groq 8K ctx)
        if len(recettes_filtered) > 30:
            recettes_sample = random.sample(recettes_filtered, 30)
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
                    break

        # 5. Sauvegarder le planning (sans liste de courses pour l'instant)
        data = {
            "plats": plats,
            "liste_courses": [],
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
            {
                "request": request,
                "error": f"Erreur : {str(e)}",
                "repas_options": REPAS_OPTIONS,
                "tag_options": TAG_OPTIONS,
                "custom_prompt": custom_prompt,
            },
        )


@app.post("/generate-shopping/{planning_id}")
async def generate_shopping(planning_id: int, request: Request):
    """Génère la liste de courses pour un planning existant."""
    import json
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
            courses_map = {}
            for plat in plats:
                try:
                    d = await llm.extract_ingredients(plat["nom_recette"], plat.get("url", ""), planning["nb_personnes"])
                    for ing in d.get("ingredients", []):
                        k = ing["nom"].lower().strip()
                        if k in courses_map and courses_map[k]["unite"] == ing.get("unite", ""):
                            try:
                                q1 = float(str(courses_map[k]["quantite"]).replace(",", "."))
                                q2 = float(str(ing["quantite"]).replace(",", "."))
                                courses_map[k]["quantite"] = str(q1 + q2)
                            except: pass
                        else:
                            courses_map[k] = {"nom": ing["nom"], "quantite": ing.get("quantite", ""), "unite": ing.get("unite", "")}
                except: pass
            liste_courses = sorted(courses_map.values(), key=lambda x: x["nom"])

        # Ajouter les ingrédients forcés
        force = planning.get("ingredients_force", "")
        if force:
            for f in [i.strip() for i in force.split(",") if i.strip()]:
                if f.lower() not in {i["nom"].lower() for i in liste_courses}:
                    liste_courses.append({"nom": f, "quantite": "", "unite": ""})

        # Mettre à jour le planning
        import aiosqlite
        async with aiosqlite.connect(settings.database_path) as db_conn:
            planning_data["liste_courses"] = liste_courses
            await db_conn.execute(
                "UPDATE planning_history SET data_json = ? WHERE id = ?",
                (json.dumps(planning_data, ensure_ascii=False), planning_id),
            )
            await db_conn.commit()

        # Sauvegarder les ingrédients dans Notion pour chaque recette
        for plat in plats:
            nid = plat.get("notion_id", "")
            nom = plat.get("nom_recette", "")
            if nid:
                # Chercher les ingrédients de cette recette dans la liste
                recette_ings = [i for i in liste_courses if i.get("nom")]
                if recette_ings:
                    nb = planning.get("nb_personnes", 4)
                    txt = f"Pour {nb} personnes :\n" + "\n".join(
                        f"- {i['nom']}" + (f" : {i['quantite']} {i['unite']}" if i.get('quantite') else "")
                        for i in recette_ings
                    )
                    try:
                        await notion.update_ingredients(nid, txt)
                    except Exception as e:
                        logger.warning(f"Impossible de sauvegarder les ingrédients pour {nom}: {e}")

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
        if note not in ("⭐", "⭐⭐", "⭐⭐⭐", "⭐⭐⭐⭐", "⭐⭐⭐⭐⭐"):
            return {"error": "Note invalide"}
        await notion.update_rating(page_id, note)
        return {"success": True}
    except Exception as e:
        logger.exception("Erreur notation")
        return {"error": str(e)}


@app.post("/api/analyze-url")
async def api_analyze_url(request: Request):
    """Analyse une URL et retourne les infos extraites."""
    import json
    try:
        data = await request.json()
        url = data.get("url", "")
        if not url:
            return {"error": "URL manquante"}

        # Extraire les infos générales
        info = await llm.extract_recipe_from_url(url)
        nom = info.get("nom", "")
        repas = info.get("type_repas", "")
        tags = info.get("tags", [])

        # Extraire les ingrédients
        ingredients = ""
        if nom:
            try:
                d = await llm.extract_ingredients(nom, url, 4)
                ings = d.get("ingredients", [])
                if ings:
                    ingredients = "\n".join(
                        f"- {i['nom']}" + (f" : {i.get('quantite','')} {i.get('unite','')}" if i.get('quantite') else "")
                        for i in ings
                    )
            except: pass

        return {
            "nom": nom,
            "repas": repas,
            "tags": tags,
            "ingredients": ingredients,
            "instructions": info.get("instructions", ""),
            "moment": "Les deux",
        }

    except Exception as e:
        logger.exception("Erreur analyse URL")
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
                import json
                try:
                    ing_list = json.loads(cached["ingredients"])
                    if ing_list:
                        ingredients_txt = "\n".join(
                            f"- {i['nom']}" + (f" : {i.get('quantite','')} {i.get('unite','')}" if i.get('quantite') else "")
                            for i in ing_list
                        )
                except: pass

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
                except: pass

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
    instructions_manual: str = Form(""),
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
                moment=moment,
            )
            page_id = result.get("id", "")
            recette_url = result.get("url", "")
            success = f"Recette « {nom} » ajoutée avec succès !"
            logger.info(f"Recette ajoutée: {nom} → {recette_url}")

            # Sauvegarder les ingrédients et instructions saisis manuellement
            if page_id and (ingredients_manual or instructions_manual):
                try:
                    if ingredients_manual:
                        await notion.update_ingredients(page_id, ingredients_manual)
                    # Sauvegarder en cache local
                    import json
                    if ingredients_manual:
                        ings_list = [
                            {"nom": l.strip().lstrip("- ").split(":")[0].strip(), "quantite": "", "unite": ""}
                            for l in ingredients_manual.split("\n") if l.strip()
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
    import json

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
                        break
                trouve = True
                break

        if not trouve:
            return {"error": "Repas non trouvé dans le planning"}

        # Regénérer la liste de courses
        liste_courses = []
        courses_map = {}

        for plat in plats:
            try:
                ingredients_data = await llm.extract_ingredients(
                    plat["nom_recette"],
                    plat.get("url", ""),
                    planning.get("nb_personnes", 4),
                )
                for ing in ingredients_data.get("ingredients", []):
                    nom_ing = ing["nom"].lower().strip()
                    if nom_ing in courses_map:
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
            except Exception:
                pass

        liste_courses = sorted(courses_map.values(), key=lambda x: x["nom"])
        planning_data["liste_courses"] = liste_courses

        # Sauvegarder
        await db.save_planning(
            week_start=planning["week_start"],
            saison=planning["saison"],
            nb_personnes=planning["nb_personnes"],
            ingredients_force=planning.get("ingredients_force", ""),
            data_json=json.dumps(planning_data, ensure_ascii=False),
            recipes=[{"notion_id": p.get("notion_id", ""), "recipe_name": p["nom_recette"], "repas_type": p.get("type_repas", ""), "jour": p["jour"], "moment": p["moment"]} for p in plats],
        )

        return {"success": True, "liste_courses": liste_courses, "plats": plats}

    except Exception as e:
        logger.exception("Erreur update meal")
        return {"error": str(e)}


@app.get("/health")
async def health():
    return {"status": "ok", "app": settings.app_title}
