"""Client LLM — supporte Ollama (local) et Gemini (cloud)."""

import asyncio
import html
import httpx
import ipaddress
import json
import logging
import random
import re
import socket
import time
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError, field_validator
from urllib.parse import urlparse

from app.config import (
    BASE_OPTIONS,
    REPAS_OPTIONS,
    TAG_OPTIONS,
    settings,
)
from app.text_utils import clean_recipe_title

logger = logging.getLogger(__name__)


# ── Robustesse des appels LLM ────────────────────────────────────────
# Un seul point d'étranglement (`_chat` + `_complete`) : retry/backoff sur
# erreurs transitoires, fallback ollama seulement si pertinent, validation
# pydantic de la sortie, sinon `LLMError`. Constantes en dur (app perso).
_MAX_RETRIES = 3
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_FATAL_STATUS = {400, 401, 403, 404}   # erreurs client : inutile de réessayer
_BACKOFF_BASE = 1.0                    # secondes (exponentiel : 1, 2, 4)
_BACKOFF_CAP = 8.0
_BACKOFF_JITTER = 0.5


class LLMError(Exception):
    """Échec d'un appel LLM après retries/validation (transport ou sortie invalide)."""


class RecipeExtraction(BaseModel):
    """Sortie d'extraction d'une recette (URL ou texte collé)."""
    nom: str = ""
    type_repas: str = ""
    tags: list[str] = Field(default_factory=list)
    base: list[str] = Field(default_factory=list)
    ingredients: list[str] = Field(default_factory=list)
    instructions: str = ""

    @field_validator("nom", "type_repas", "instructions", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, list):  # le LLM renvoie parfois instructions en liste
            return "\n".join(str(x).strip() for x in v if str(x).strip())
        return str(v)

    @field_validator("ingredients", mode="before")
    @classmethod
    def _coerce_ing_list(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [l.strip() for l in v.split("\n") if l.strip()]
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return []

    @field_validator("tags", "base", mode="before")
    @classmethod
    def _coerce_tags(cls, v: Any) -> list[str]:
        if isinstance(v, list):
            return [str(x) for x in v]
        return [str(v)] if v else []


class IngredientItem(BaseModel):
    nom: str = ""
    quantite: str = ""
    unite: str = ""

    @field_validator("nom", "quantite", "unite", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return "" if v is None else str(v)


class IngredientsResponse(BaseModel):
    """Sortie d'extraction d'ingrédients d'une recette."""
    ingredients: list[IngredientItem] = Field(default_factory=list)
    cuisson_minutes: int | None = None

    @field_validator("ingredients", mode="before")
    @classmethod
    def _drop_non_dict(cls, v: Any) -> list:
        return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []

    @field_validator("cuisson_minutes", mode="before")
    @classmethod
    def _int_or_none(cls, v: Any) -> int | None:
        try:
            return int(v)
        except (TypeError, ValueError):
            return None


class ClassifyResponse(BaseModel):
    """Sortie de classification type_repas + tags + base."""
    type_repas: str = ""
    tags: list[str] = Field(default_factory=list)
    base: list[str] = Field(default_factory=list)

    @field_validator("type_repas", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return "" if v is None else str(v)

    @field_validator("tags", "base", mode="before")
    @classmethod
    def _coerce_tags(cls, v: Any) -> list[str]:
        if isinstance(v, list):
            return [str(x) for x in v]
        return [str(v)] if v else []

# Garde anti-SSRF : on ne fetch que du http(s) public, jamais une IP interne.
_MAX_FETCH_BYTES = 5 * 1024 * 1024  # 5 Mo : refus des réponses trop grosses
_FETCH_TIMEOUT = 8.0


async def _is_public_http_url(url: str) -> bool:
    """Vrai si l'URL est http(s) et que TOUTES ses IP résolues sont publiques
    (bloque localhost, 169.254.x, 10/172.16/192.168, etc.)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, p.hostname, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


async def _safe_fetch_html(url: str) -> str:
    """Télécharge le HTML d'une URL publique, avec cap de taille et timeout.
    Retourne "" si l'URL est interne/illégitime ou en cas d'erreur."""
    if not await _is_public_http_url(url):
        logger.warning(f"URL rejetée (non publique / schéma invalide) : {url}")
        return ""
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=True) as client:
            async with client.stream("GET", url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }) as resp:
                resp.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_FETCH_BYTES:
                        logger.warning(f"Réponse trop grosse (> {_MAX_FETCH_BYTES} o), tronquée : {url}")
                        break
                    chunks.append(chunk)
                return b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"Impossible de récupérer la page {url}: {e}")
        return ""

SYSTEM_PROMPT_INGREDIENTS = """Tu es un assistant culinaire. Pour une recette donnée (nom + éventuellement URL), liste les ingrédients nécessaires.

RÈGLES :
- Si un contenu de page ou une liste d'ingrédients est fourni, BASE-TOI dessus
  fidèlement (n'invente pas, n'ajoute pas d'ingrédients absents).
- Sépare quantité / unité / nom (ex: "300 g de lentilles" → quantite "300", unite "g", nom "lentilles").
- Adapte les quantités au nombre de personnes indiqué.
- Regroupe les ingrédients similaires (ex: "oignons" même si utilisé plusieurs fois).

Répond UNIQUEMENT avec ce JSON :
{"ingredients": [{"nom": "...", "quantite": "...", "unite": "..."}]}

Pas de texte avant ou après le JSON."""


def _clean_text(s: str) -> str:
    """Décode les entités HTML et retire d'éventuelles balises résiduelles."""
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"[ \t]+", " ", s).strip()


def _first_image(val: Any) -> str:
    """image JSON-LD peut être une str, un objet {url}, ou une liste de ceux-ci."""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return val.get("url", "") or val.get("contentUrl", "")
    if isinstance(val, list) and val:
        return _first_image(val[0])
    return ""


def _is_garbage(t: str) -> bool:
    """Détecte un texte poubelle (ex. 'Array,Array,Array...' mal sérialisé)."""
    return bool(re.fullmatch(r"(array[\s,]*)+", t, re.IGNORECASE))


def _flatten_instructions(val: Any) -> list[str]:
    """recipeInstructions : str | [str] | [HowToStep{text}] | [HowToSection{itemListElement}]."""
    steps: list[str] = []
    if isinstance(val, str):
        # parfois un seul bloc avec sauts de ligne
        for part in re.split(r"\n+|(?<=[.!?])\s{2,}", val):
            t = _clean_text(part)
            if t and not _is_garbage(t):
                steps.append(t)
    elif isinstance(val, list):
        for item in val:
            if isinstance(item, str):
                t = _clean_text(item)
                if t and not _is_garbage(t):
                    steps.append(t)
            elif isinstance(item, dict):
                if item.get("@type") == "HowToSection" and "itemListElement" in item:
                    steps.extend(_flatten_instructions(item["itemListElement"]))
                else:
                    # 'text' d'abord ; 'name' seulement s'il n'est pas du garbage
                    t = _clean_text(item.get("text") or "")
                    if not t:
                        nm = _clean_text(item.get("name") or "")
                        t = nm if nm and not _is_garbage(nm) else ""
                    if t and not _is_garbage(t):
                        steps.append(t)
    return steps


_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


_ING_HINT = re.compile(r"^\s*[\d½¼¾]|(\b(?:g|kg|mg|ml|cl|dl|l|cuill|pinc|gousse|tranche|sachet|càs|càc|c\.)\b)", re.IGNORECASE)


_PREP_MARKERS = ("préparation", "preparation", "instruction", "étape", "etape", "réalisation", "realisation")


def _join_fragments(lines: list[str]) -> list[str]:
    """Rassemble les fragments d'une même phrase coupés par virgule.

    Certains sites découpent une phrase en plusieurs « étapes » (ex.
    « Épluchez l'oignon » / « l'ail et les carottes... »). On accumule jusqu'à
    une ponctuation forte (. ! ? … :) → une étape = une vraie phrase."""
    out: list[str] = []
    buf = ""
    for l in lines:
        l = l.strip()
        if not l:
            continue
        buf = f"{buf}, {l}" if buf else l
        if re.search(r"[.!?…:]$", buf):
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def _split_blob(blob: str) -> tuple[list[str], list[str]]:
    """Découpe un bloc texte en (ingrédients, instructions).

    Ingrédients = lignes entre un titre « Ingrédient(s) » et « Préparation » ;
    instructions = lignes après « Préparation/Instructions ». Couvre les sites
    qui mettent tout en texte (séparé \\n/\\t) ou mal taguent leur JSON-LD
    (ex. platetrecette : tout fourré dans recipeIngredient)."""
    if not blob:
        return [], []
    lines = [l.strip(" -•\t\xa0") for l in re.split(r"[\n\r]+", blob.replace("\t", "\n"))]
    ings: list[str] = []
    instrs: list[str] = []
    mode = None
    for l in lines:
        low = l.lower()
        if "ingr" in low and "dient" in low and len(l) < 60:
            mode = "ing"
            continue
        if any(k in low for k in _PREP_MARKERS) and len(l) < 40:
            mode = "instr"
            continue
        if not l:
            continue
        if mode == "ing" and len(l) <= 120:
            ings.append(l)
        elif mode == "instr":
            instrs.append(l)
    return ings, _join_fragments(instrs)


def _ingredients_from_text(blob: str) -> list[str]:
    """Ingrédients seuls d'un bloc texte (cf. _split_blob)."""
    if not blob or "ngr" not in blob.lower():
        return []
    return _split_blob(blob)[0]


def _scrape_ingredient_list(html_text: str) -> list[str]:
    """Récupère la liste <ul><li> d'ingrédients du HTML, sans LLM. Utile quand le
    JSON-LD n'expose pas recipeIngredient (ex. platetrecette.fr).

    On évalue chaque <ul> suivant un « Ingrédient » et on retient celle dont les
    items ressemblent vraiment à des ingrédients (chiffres/unités) — pas un menu
    de navigation.
    """
    best: list[str] = []
    best_score = 0
    for m in re.finditer(r"[Ii]ngr[ée]dient", html_text):
        window = html_text[m.start():m.start() + 4000]
        lm = re.search(r"<ul[^>]*>(.*?)</ul>", window, re.S | re.I)
        if not lm:
            continue
        items = [c for c in (_clean_text(x) for x in re.findall(r"<li[^>]*>(.*?)</li>", lm.group(1), re.S | re.I)) if c and len(c) <= 120]
        if len(items) < 2:
            continue
        score = sum(1 for c in items if _ING_HINT.search(c))
        if score > best_score:
            best_score, best = score, items
    # au moins la moitié des items doivent ressembler à des ingrédients
    if best and best_score >= max(2, len(best) // 2):
        return best
    return []


def _visible_text(html_text: str) -> str:
    """Texte visible d'une page (balises script/style/html retirées)."""
    t = re.sub(r"<script[^>]*>.*?</script>", "", html_text, flags=re.DOTALL | re.IGNORECASE)
    t = re.sub(r"<style[^>]*>.*?</style>", "", t, flags=re.DOTALL | re.IGNORECASE)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _balanced_json(text: str, start: int) -> str | None:
    """Extrait l'objet JSON équilibré à partir d'un '{' à l'index `start`.

    Gère les chaînes et les échappements (un '}' dans une chaîne ne compte pas).
    """
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def _find_enclosing_start(text: str, idx: int) -> int:
    """Remonte depuis `idx` jusqu'au '{' qui ouvre l'objet le contenant.

    Compte les accolades à l'envers pour sauter les objets imbriqués déjà
    fermés (ex. un VideoObject listé avant la clé "@context" du parent).
    """
    depth = 0
    i = idx
    while i >= 0:
        ch = text[i]
        if ch == "}":
            depth += 1
        elif ch == "{":
            if depth == 0:
                return i
            depth -= 1
        i -= 1
    return -1


def _jsonld_candidates(html_text: str) -> list[str]:
    """Rassemble les objets JSON schema.org de la page.

    1) blocs <script type="application/ld+json"> classiques.
    2) repli robuste : tout objet commençant par {"@context"...} repéré dans le
       HTML brut et extrait par équilibrage d'accolades. Couvre les sites (ex.
       Marmiton) où l'attribut type est encodé en entités HTML ou le JSON inline.
    """
    out: list[str] = []
    out += re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text, re.DOTALL | re.IGNORECASE,
    )
    # Repli : ancrer sur "@context" puis remonter au '{' ouvrant de son objet.
    # Couvre les pages où @context n'est pas la première clé (ex. Ricardo) ou
    # où l'attribut type du <script> est encodé en entités HTML (ex. Marmiton).
    seen_starts: set[int] = set()
    for m in re.finditer(r'"@context"', html_text):
        start = _find_enclosing_start(html_text, m.start())
        if start < 0 or start in seen_starts:
            continue
        seen_starts.add(start)
        obj = _balanced_json(html_text, start)
        if obj:
            out.append(obj)
    return out


def _num(value) -> float | None:
    """Extrait le 1er nombre d'une valeur ("250 kcal" -> 250)."""
    m = re.search(r"[\d]+(?:[.,]\d+)?", str(value))
    return float(m.group().replace(",", ".")) if m else None


def _extract_nutrition(node: dict) -> dict:
    """Nutrition schema.org (NutritionInformation, par part) -> dict normalisé."""
    n = node.get("nutrition")
    if not isinstance(n, dict):
        return {}
    out: dict[str, float] = {}
    mapping = {
        "calories": "calories", "proteinContent": "proteines",
        "carbohydrateContent": "glucides", "fatContent": "lipides",
    }
    for src, dst in mapping.items():
        v = _num(n.get(src))
        if v is not None:
            out[dst] = round(v) if dst == "calories" else round(v, 1)
    # On ne garde la nutrition de la source que si elle est COMPLÈTE (calories +
    # 3 macros). Sinon (souvent juste calories, parfois fausses) on laissera
    # l'estimation calculée prendre le relais.
    if not {"calories", "proteines", "glucides", "lipides"} <= out.keys():
        return {}
    return out


def _extract_jsonld_recipe(html_text: str) -> dict[str, Any] | None:
    """Renvoie la 1re recette schema.org de la page (script ld+json OU inline).

    Source la plus fiable : pas de LLM, données telles que publiées par le site.
    """
    for block in _jsonld_candidates(html_text):
        raw = block.strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        # Aplatir les structures possibles : objet, liste, ou {"@graph": [...]}.
        # NB : on inclut TOUJOURS la racine ET les noeuds @graph — certains sites
        # (ex. Ricardo) mettent le Recipe à la racine MAIS ajoutent aussi un
        # @graph (WebSite). Remplacer la racine par @graph perdait le Recipe.
        candidates: list[Any] = []
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            candidates = [data]
            graph = data.get("@graph")
            if isinstance(graph, list):
                candidates += graph
        for node in candidates:
            if not isinstance(node, dict):
                continue
            t = node.get("@type", "")
            types = t if isinstance(t, list) else [t]
            if not any("Recipe" in str(x) for x in types):
                continue
            # Certains sites (ex. CuisineActuelle) glissent un en-tête
            # "Ingrédients" comme 1re entrée du tableau : on l'écarte.
            _NOISE = {"ingrédients", "ingredients", "ingrédient", "ingredient"}
            raw_ings = node.get("recipeIngredient") or node.get("ingredients") or []
            ingredients = [
                c for c in (_clean_text(i) for i in raw_ings)
                if c and c.lower() not in _NOISE
            ]
            instructions = _flatten_instructions(node.get("recipeInstructions", ""))

            # Cas "blob mal tagué" (ex. platetrecette) : tout est fourré dans
            # recipeIngredient en texte multi-lignes (intro + ingrédients +
            # préparation). On re-découpe le blob recipeIngredient seul (pas
            # recipeInstructions qui en est souvent un doublon).
            blob = "\n".join(str(i) for i in raw_ings)
            looks_blob = any(("\n" in str(i) or "\r" in str(i) or len(str(i)) > 90) for i in raw_ings)
            if (looks_blob or not ingredients) and "ingr" in blob.lower():
                b_ings, b_instr = _split_blob(blob)
                if len(b_ings) >= 2:
                    seen, deduped = set(), []
                    for x in b_ings:
                        if x.lower() not in seen:
                            seen.add(x.lower())
                            deduped.append(x)
                    ingredients = deduped
                    if b_instr:
                        instructions = b_instr
            # Complément via la description (recette/vidéo) : ces sites y mettent
            # souvent la liste COMPLÈTE + la préparation, alors que
            # recipeIngredient est partiel et recipeInstructions vide/garbage.
            # On préfère le blob s'il a PLUS d'ingrédients, et on remplit les
            # instructions si elles manquent.
            blobs = [node.get("description", "")]
            blobs += [v.get("description", "") for v in (node.get("video") or []) if isinstance(v, dict)]
            for b in blobs:
                b_ings, b_instr = _split_blob(b)
                if len(b_ings) > len(ingredients):
                    ingredients = b_ings
                if not instructions and b_instr:
                    instructions = b_instr
            kw = node.get("keywords", "")
            keywords = [k.strip() for k in kw.split(",")] if isinstance(kw, str) else [
                _clean_text(k) for k in (kw or [])
            ]
            cat = node.get("recipeCategory", "")
            if isinstance(cat, str) and cat:
                keywords.append(cat)
            return {
                "nom": clean_recipe_title(_clean_text(node.get("name", ""))),
                "image_url": _first_image(node.get("image", "")),
                "ingredients": ingredients,
                "instructions": "\n".join(instructions),
                "keywords": [k for k in keywords if k],
                "nutrition": _extract_nutrition(node),
                "duree_minutes": _recipe_duration_min(node),
            }
    return None


def _iso_duration_to_min(val: Any) -> int | None:
    """Convertit une durée ISO-8601 (ex. « PT1H30M », « PT45M ») en minutes."""
    if not isinstance(val, str):
        return None
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?", val.strip(), re.IGNORECASE)
    if not m or not (m.group(1) or m.group(2)):
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def _recipe_duration_min(node: dict) -> int | None:
    """Durée totale d'une recette : totalTime, sinon prepTime + cookTime."""
    total = _iso_duration_to_min(node.get("totalTime"))
    if total:
        return total
    prep = _iso_duration_to_min(node.get("prepTime")) or 0
    cook = _iso_duration_to_min(node.get("cookTime")) or 0
    return (prep + cook) or None


# ── Handlers spécifiques par site ────────────────────────────────────
# Pour les sites SANS JSON-LD exploitable. Un handler reçoit le HTML brut et
# renvoie le MÊME dict que _extract_jsonld_recipe
#   {nom, image_url, ingredients: list[str], instructions: str, keywords: list[str]}
# ou None s'il échoue. Ordre d'extraction : JSON-LD générique → handler de
# domaine → repli LLM. Ajouter un site = écrire une fonction + l'enregistrer ci-dessous.
#
# Exemple :
#   def _handler_monsite(html_text: str) -> dict[str, Any] | None:
#       nom = _clean_text(re.search(r'<h1[^>]*>(.*?)</h1>', html_text, re.S).group(1))
#       ings = [_clean_text(x) for x in re.findall(r'<li class="ingredient">(.*?)</li>', html_text, re.S)]
#       if not ings:
#           return None
#       return {"nom": nom, "image_url": "", "ingredients": ings, "instructions": "", "keywords": []}
#   SITE_HANDLERS = {"monsite.com": _handler_monsite}

def _handler_amandinecooking(html_text: str) -> dict[str, Any] | None:
    """amandinecooking.com : blog WordPress sans schema.org.

    Structure : <h2>Titre</h2> ... <h2>Ingrédients ...</h2><ul><li>...</li></ul>
    ... <h2>Préparation</h2><ol><li>...</li></ol>. Image via og:image (repli).
    """
    def items_after(label: str) -> list[str]:
        idx = html_text.find(label)
        if idx < 0:
            return []
        lm = re.search(r"<(ul|ol)[^>]*>(.*?)</\1>", html_text[idx:], re.S | re.I)
        if not lm:
            return []
        lis = re.findall(r"<li[^>]*>(.*?)</li>", lm.group(2), re.S | re.I)
        return [t for t in (_clean_text(x) for x in lis) if t]

    ingredients = items_after("Ingr&eacute;dients")
    instructions = items_after("Pr&eacute;paration")
    if not ingredients:
        return None

    # Titre : premier <h2> qui n'est ni Ingrédients ni Préparation.
    nom = ""
    for h in re.findall(r"<h2[^>]*>(.*?)</h2>", html_text, re.S | re.I):
        t = _clean_text(h)
        if t and "ngr" not in t.lower() and "paration" not in t.lower():
            nom = t
            break
    if not nom:
        m = re.search(r"<title>(.*?)</title>", html_text, re.S | re.I)
        nom = _clean_text(m.group(1)).split(" - ")[0] if m else ""

    return {
        "nom": clean_recipe_title(nom),
        "image_url": "",  # repli sur og:image côté extract_recipe_from_url
        "ingredients": ingredients,
        "instructions": "\n".join(instructions),
        "keywords": [],
    }


SITE_HANDLERS: dict[str, Callable[[str], dict[str, Any] | None]] = {
    "amandinecooking.com": _handler_amandinecooking,
}


def _site_handler(url: str) -> Callable[[str], dict[str, Any] | None] | None:
    """Renvoie le handler enregistré pour le domaine de l'URL (ou None)."""
    domain = urlparse(url).netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return SITE_HANDLERS.get(domain)


class LLMClient:
    """Client LLM multi-provider (Ollama ou Gemini)."""

    def __init__(self) -> None:
        self.provider = settings.llm_provider
        self._setup_provider()

    def _setup_provider(self) -> None:
        if self.provider == "gemini":
            self._api_key = settings.gemini_api_key
            self._model = settings.gemini_model
            self._url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self._model}:generateContent?key={self._api_key}"
            )
        elif self.provider == "groq":
            self._api_key = settings.groq_api_key
            self._model = settings.groq_model
            self._url = "https://api.groq.com/openai/v1/chat/completions"
        else:
            # Ollama (default)
            self._url = f"{settings.ollama_url}/api/chat"
            self._model = settings.ollama_model

    @staticmethod
    def _log_usage(provider: str, label: str, in_tok: int, out_tok: int) -> None:
        """Trace la consommation de tokens pour pouvoir l'optimiser/surveiller."""
        if in_tok or out_tok:
            logger.info(
                f"🔢 {provider}/{label}: {in_tok} in + {out_tok} out = {in_tok + out_tok} tokens"
            )

    async def _chat_call(
        self, provider: str, system: str, user: str, temperature: float,
        max_tokens: int, label: str, json_mode: bool,
    ) -> str:
        """Dispatch bas niveau vers un provider donné (sans retry ni fallback)."""
        if provider == "gemini":
            return await self._chat_gemini(system, user, temperature, max_tokens, label, json_mode)
        if provider == "groq":
            return await self._chat_groq(system, user, temperature, max_tokens, label, json_mode)
        return await self._chat_ollama(system, user, temperature, max_tokens, json_mode)

    @staticmethod
    def _is_fatal(exc: Exception) -> bool:
        """Erreur client non récupérable (clé invalide, mauvaise requête) : ne pas réessayer."""
        return (isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code in _FATAL_STATUS)

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """Erreur transitoire : timeout, coupure réseau, 429/5xx, réponse vide."""
        if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in _RETRYABLE_STATUS
        return isinstance(exc, ValueError)  # ex. Gemini « aucune réponse »

    @staticmethod
    def _retry_after(exc: Exception) -> float | None:
        """Délai imposé par l'en-tête Retry-After d'un 429, si présent."""
        if isinstance(exc, httpx.HTTPStatusError):
            ra = exc.response.headers.get("Retry-After")
            if ra:
                try:
                    return float(ra)
                except ValueError:
                    return None
        return None

    async def _chat(
        self, system: str, user: str, temperature: float = 0.3,
        max_tokens: int = 2048, label: str = "chat", json_mode: bool = False,
    ) -> str:
        """Appel LLM durci : retry/backoff sur transitoire, fallback ollama si
        pertinent, sinon `LLMError`. Point d'étranglement unique de tous les appels."""
        last: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            t0 = time.monotonic()
            try:
                raw = await self._chat_call(
                    self.provider, system, user, temperature, max_tokens, label, json_mode)
                self._log_call(self.provider, label, attempt, t0, "ok")
                return raw
            except Exception as e:
                if self._is_fatal(e):
                    self._log_call(self.provider, label, attempt, t0, "fatal")
                    raise LLMError(f"{self.provider}/{label}: erreur non récupérable ({e})") from e
                last = e
                self._log_call(self.provider, label, attempt, t0, "échec")
                if not self._is_retryable(e) or attempt == _MAX_RETRIES:
                    break
                delay = (self._retry_after(e)
                         or min(_BACKOFF_CAP, _BACKOFF_BASE * 2 ** (attempt - 1)))
                delay += random.uniform(0, _BACKOFF_JITTER)
                logger.warning(f"⚠️ {self.provider}/{label} tentative {attempt} KO ({e}); "
                               f"retry dans {delay:.1f}s")
                await asyncio.sleep(delay)

        # Provider principal épuisé → fallback ollama seulement si pertinent.
        if self.provider in ("gemini", "groq") and settings.ollama_url:
            logger.warning(f"⚠️ {self.provider}/{label} indisponible, fallback ollama…")
            t0 = time.monotonic()
            try:
                raw = await self._chat_call("ollama", system, user, temperature, max_tokens, label, json_mode)
                self._log_call("ollama", label, 1, t0, "fallback")
                return raw
            except Exception as e:
                raise LLMError(f"{self.provider}/{label}: fallback ollama échoué ({e})") from e
        raise LLMError(f"{self.provider}/{label} indisponible après {_MAX_RETRIES} tentatives ({last})") from last

    @staticmethod
    def _log_call(provider: str, label: str, attempt: int, t0: float, outcome: str) -> None:
        """Log structuré par appel (provider, issue, latence, tentative)."""
        ms = int((time.monotonic() - t0) * 1000)
        logger.info(f"🤖 {provider}/{label}: {outcome} — {ms}ms (tentative {attempt})")

    async def _complete(
        self, system: str, user: str, *, response_model: type[BaseModel], label: str,
        temperature: float = 0.1, max_tokens: int = 1024, core_field: str | None = None,
    ) -> BaseModel:
        """Appel LLM + parse + validation pydantic, avec 1 retry de réparation.

        Renvoie une instance validée de `response_model`, ou lève `LLMError`
        (transport épuisé via `_chat`, ou sortie invalide après réparation).
        `core_field` : si ce champ est vide au 1er essai, on retente une fois."""
        strict = ("\n\nIMPORTANT : réponds UNIQUEMENT par un objet JSON valide "
                  "conforme au schéma demandé, sans texte ni ``` autour.")
        last_err: Exception | None = None
        for attempt in (1, 2):
            raw = await self._chat(
                system, user if attempt == 1 else user + strict,
                temperature=temperature, max_tokens=max_tokens, label=label, json_mode=True)
            data = self._parse_json(raw)
            try:
                model = response_model.model_validate(data if isinstance(data, dict) else {})
            except ValidationError as e:
                last_err = e
                logger.warning(f"{label}: JSON invalide (tentative {attempt}): {e.errors()[:2]}")
                continue
            # Champ cœur vide = extraction ratée (JSON vide/hors-sujet) : on
            # retente une fois, puis on échoue (pas de faux succès silencieux).
            if core_field and not getattr(model, core_field, None):
                last_err = ValueError(f"champ cœur « {core_field} » vide")
                logger.warning(f"{label}: {last_err} (tentative {attempt})")
                continue
            return model
        raise LLMError(f"{label}: sortie LLM invalide après réparation ({last_err})") from last_err

    async def _chat_ollama(
        self, system: str, user: str, temperature: float = 0.3,
        max_tokens: int = 2048, json_mode: bool = False,
    ) -> str:
        url = f"{settings.ollama_url}/api/chat"
        payload = {
            "model": settings.ollama_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            self._log_usage("ollama", "chat", data.get("prompt_eval_count", 0), data.get("eval_count", 0))
            return data["message"]["content"]

    async def _chat_groq(
        self, system: str, user: str, temperature: float = 0.3,
        max_tokens: int = 2048, label: str = "chat", json_mode: bool = False,
    ) -> str:
        """Groq (API compatible OpenAI). Avec gestion rate limit."""
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        # Le retry/backoff (dont 429) est géré centralement par `_chat`.
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(self._url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            usage = data.get("usage", {})
            self._log_usage("groq", label, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            return data["choices"][0]["message"]["content"]

    async def _chat_gemini(
        self, system: str, user: str, temperature: float = 0.3,
        max_tokens: int = 2048, label: str = "chat", json_mode: bool = False,
    ) -> str:
        gen_config = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if json_mode:
            gen_config["responseMimeType"] = "application/json"
        payload = {
            "contents": [{"parts": [{"text": f"{system}\n\n{user}"}]}],
            "generationConfig": gen_config,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(self._url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            usage = data.get("usageMetadata", {})
            self._log_usage("gemini", label, usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0))
            candidates = data.get("candidates", [])
            if not candidates:
                raise ValueError("Gemini: aucune réponse")
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)

    # ── Extraction ingrédients ───────────────────────────────────

    async def extract_ingredients(
        self,
        nom_recette: str,
        url: str = "",
        nb_personnes: int = 4,
    ) -> dict[str, Any]:
        # Récupérer le contenu réel de la recette plutôt que de laisser le LLM
        # inventer à partir du seul nom. On privilégie les ingrédients JSON-LD
        # (exacts), sinon le texte de la page.
        context = ""
        if url:
            try:
                async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                    resp = await client.get(url, headers={"User-Agent": _UA})
                if resp.status_code < 400:
                    ld = _extract_jsonld_recipe(resp.text)
                    if ld and ld.get("ingredients"):
                        context = ("Ingrédients EXACTS de la page (structure-les fidèlement, "
                                   "sans rien inventer ni ajouter) :\n" + "\n".join(ld["ingredients"]))
                    else:
                        txt = _visible_text(resp.text)[:3000]
                        if txt:
                            context = "Contenu de la page :\n" + txt
            except Exception as e:
                logger.debug(f"Fetch ingrédients {url}: {e}")

        user_prompt = f"""Recette : {nom_recette}
Nombre de personnes : {nb_personnes}

{context or "Aucun contenu disponible : utilise tes connaissances sur cette recette."}

Donne les ingrédients pour {nb_personnes} personnes, en quantités adaptées."""

        # Appel durci + validation pydantic (transport/retry/fallback via _chat).
        resp: IngredientsResponse = await self._complete(
            SYSTEM_PROMPT_INGREDIENTS, user_prompt, response_model=IngredientsResponse,
            label="ingredients", temperature=0.1, max_tokens=512, core_field="ingredients",
        )
        return {
            "ingredients": [i.model_dump() for i in resp.ingredients],
            "cuisson_minutes": resp.cuisson_minutes or 30,
        }

    # ── Classification type + tags ───────────────────────────────
    # Indices lexicaux pour corriger/compléter la classification LLM.
    _SAVORY_WORDS = {
        "salé", "salée", "saumon", "thon", "jambon", "lardon", "poulet", "boeuf",
        "bœuf", "porc", "veau", "crevette", "poireau", "courgette", "épinard",
        "parmesan", "chèvre", "feta", "quiche", "gratin", "curry", "lentille",
    }
    _SWEET_WORDS = {
        "sucre", "chocolat", "vanille", "framboise", "fraise", "caramel", "miel",
        "gâteau", "gateau", "biscuit", "compote", "confiture", "tiramisu", "crêpe",
    }
    # Indices lexicaux pour deviner la Base (ingrédient principal). Ordre non
    # significatif : plusieurs bases peuvent coexister (ex. viande + féculent).
    _BASE_WORDS = {
        "Viande": ("poulet", "boeuf", "bœuf", "porc", "veau", "jambon", "lardon",
                   "agneau", "dinde", "canard", "saucisse", "steak", "viande"),
        "Poisson": ("saumon", "thon", "cabillaud", "poisson", "crevette", "colin",
                    "truite", "sardine", "merlu", "lieu", "crustac"),
        "Œuf": ("oeuf", "œuf"),
        "Légume": ("courgette", "épinard", "epinard", "poireau", "carotte",
                   "tomate", "légume", "legume", "brocoli", "aubergine", "haricot"),
        "Féculent": ("pâtes", "pates", "riz", "pomme de terre", "semoule",
                     "quinoa", "lentille", "blé", "boulgour", "gnocchi", "polenta"),
        "Végé": ("tofu", "pois chiche", "seitan", "tempeh"),
    }

    def _keyword_guess(
        self, nom: str, ingredients: list[str], keywords: list[str],
    ) -> tuple[str, list[str], list[str], bool, bool]:
        """Heuristique sans LLM : (type_repas deviné, tags par mots-clés, base
        devinée, savory?, sweet?). Sert de repli ET de garde-fou contre le LLM."""
        text = (nom + " " + " ".join(ingredients[:15]) + " " + " ".join(keywords)).lower()
        savory = any(w in text for w in self._SAVORY_WORDS)
        sweet = any(w in text for w in self._SWEET_WORDS)
        if "apér" in text or "apéro" in text:
            t = "Apéro"
        elif any(w in text for w in ("boisson", "cocktail", "smoothie", "jus de")):
            t = "Boisson"
        elif "goûter" in text or "gouter" in text:
            t = "Goûter"
        elif "petit déj" in text or "petit-déj" in text or "petit dej" in text:
            t = "Petit dej"
        elif sweet and not savory:
            t = "Dessert"
        else:
            t = "Plat"
        kw_lower = {k.lower() for k in keywords}
        tags = [tag for tag in TAG_OPTIONS if tag.lower() in kw_lower]
        # Détection par MOT ENTIER (\b) : « boeuf » ne doit pas déclencher « Œuf ».
        base = [
            b for b, words in self._BASE_WORDS.items()
            if any(re.search(r"\b" + re.escape(w) + r"\b", text) for w in words)
        ]
        return t, tags, base, savory, sweet

    async def _classify_type_tags(
        self, nom: str, ingredients: list[str], keywords: list[str],
    ) -> tuple[str, list[str], list[str]]:
        """Choisit type_repas + tags + base parmi les valeurs Notion autorisées.

        Petit appel LLM (cheap) : ne touche PAS au contenu (nom/ingrédients/
        instructions restent ceux de la page), juste le classement. Un garde-fou
        lexical corrige les erreurs flagrantes (ex. cheesecake salé → pas Dessert)
        et complète les tags/base manquants."""
        guess_type, kw_tags, kw_base, savory, sweet = self._keyword_guess(nom, ingredients, keywords)
        ing_sample = ", ".join(ingredients[:12])
        prompt = f"""Recette : "{nom}"
Ingrédients : {ing_sample or "inconnus"}
Mots-clés du site : {", ".join(keywords) or "aucun"}

Classe cette recette selon TROIS dimensions distinctes :
- type_repas (cours du repas) : EXACTEMENT une valeur parmi {REPAS_OPTIONS}
- base (ingrédient principal) : 0 à 3 valeurs parmi {BASE_OPTIONS}
- tags (attributs) : 0 à 4 valeurs parmi {TAG_OPTIONS}

Réponds UNIQUEMENT ce JSON : {{"type_repas": "...", "base": ["..."], "tags": ["..."]}}"""
        try:
            resp: ClassifyResponse = await self._complete(
                "", prompt, response_model=ClassifyResponse,
                label="classify", temperature=0.0, max_tokens=180,
            )
            type_repas = resp.type_repas if resp.type_repas in REPAS_OPTIONS else ""
            # Garde-fou : un plat clairement salé ne peut pas être un Dessert.
            if type_repas == "Dessert" and savory and not sweet:
                logger.info(f"Classification corrigée : '{nom}' Dessert → {guess_type} (salé)")
                type_repas = guess_type
            if not type_repas:
                type_repas = guess_type
            # Union tags LLM + tags mots-clés (sans doublon, max 4).
            tags = [t for t in resp.tags if t in TAG_OPTIONS]
            for t in kw_tags:
                if t not in tags:
                    tags.append(t)
            # Union base LLM (filtrée) + base devinée par mots-clés.
            base = [b for b in resp.base if b in BASE_OPTIONS]
            for b in kw_base:
                if b not in base:
                    base.append(b)
            return type_repas, tags[:4], base
        except Exception as e:  # inclut LLMError : repli déterministe sur les mots-clés
            logger.warning(f"Classification échouée ({e}), repli sur mots-clés")
            return guess_type, kw_tags[:4], kw_base

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        """Parse une réponse JSON LLM (tolère fences ```)."""
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    return {}
        return {}

    # ── Extraction depuis URL ────────────────────────────────────
    async def extract_recipe_from_url(self, url: str) -> dict[str, Any]:
        """Extrait une recette d'une URL : titre, type, tags, ingrédients,
        instructions, image. Tente d'abord le JSON-LD schema.org (exact, sans
        LLM), puis repli sur une extraction LLM si la page n'en contient pas."""
        og_image = ""
        html_text = await _safe_fetch_html(url)
        if html_text:
            og_match = re.search(
                r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
                html_text, re.IGNORECASE,
            )
            if og_match:
                og_image = og_match.group(1)

        # Liste d'ingrédients scrapée du HTML (exact). Sert quand le JSON-LD
        # n'expose pas recipeIngredient.
        scraped = _scrape_ingredient_list(html_text) if html_text else []

        # 1) JSON-LD schema.org : données telles quelles, aucune déformation.
        ld = _extract_jsonld_recipe(html_text) if html_text else None
        ld_ings = (ld.get("ingredients") if ld else []) or scraped
        if ld and ld_ings:
            logger.info("Recette extraite via JSON-LD/scrape (exact, sans LLM pour les ingrédients)")
            type_repas, tags, base = await self._classify_type_tags(
                ld["nom"], ld_ings, ld.get("keywords", []),
            )
            return {
                "nom": ld["nom"],
                "type_repas": type_repas,
                "tags": tags,
                "base": base,
                "ingredients": ld_ings,                    # list[str], lignes brutes
                "instructions": ld["instructions"],
                "image_url": ld.get("image_url") or og_image,
                "nutrition": ld.get("nutrition", {}),
                "duree_minutes": ld.get("duree_minutes"),
                "source": "jsonld" if ld.get("ingredients") else "scrape",
            }

        # 1bis) Handler spécifique au domaine (sites sans JSON-LD exploitable).
        handler = _site_handler(url)
        if handler and html_text:
            try:
                h = handler(html_text)
            except Exception as e:
                logger.warning(f"Handler de domaine échoué: {e}")
                h = None
            if h and h.get("ingredients"):
                logger.info("Recette extraite via handler de domaine")
                type_repas, tags, base = await self._classify_type_tags(
                    h.get("nom", ""), h["ingredients"], h.get("keywords", []),
                )
                return {
                    "nom": h.get("nom", ""),
                    "type_repas": type_repas,
                    "tags": tags,
                    "base": base,
                    "ingredients": h["ingredients"],
                    "instructions": h.get("instructions", ""),
                    "image_url": h.get("image_url") or og_image,
                    "source": "handler",
                }

        # 2) Repli LLM : texte visible de la page (plus large car pas de JSON-LD).
        text = re.sub(r'<script[^>]*>.*?</script>', '', html_text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        page_text = re.sub(r'\s+', ' ', text).strip()[:5000]

        user_prompt = f"""Contenu d'une page de recette :

{page_text or "Contenu inaccessible, utilise tes connaissances sur cette recette."}

Extrais sans rien inventer ni reformuler :
- nom : titre exact de la recette
- type_repas (cours du repas) : une valeur parmi {REPAS_OPTIONS}
- base (ingrédient principal) : 0 à 3 valeurs parmi {BASE_OPTIONS}
- tags (attributs) : 0 à 4 valeurs parmi {TAG_OPTIONS}
- ingredients : liste des ingrédients avec quantités, recopiés tels quels (un par entrée)
- instructions : étapes de cuisson recopiées textuellement, séparées par des retours à la ligne

Réponds UNIQUEMENT ce JSON :
{{"nom": "...", "type_repas": "...", "base": ["..."], "tags": ["..."], "ingredients": ["..."], "instructions": "..."}}"""

        try:
            r: RecipeExtraction = await self._complete(
                "", user_prompt, response_model=RecipeExtraction,
                label="url-extract", temperature=0.1, max_tokens=2000, core_field="nom",
            )
        except LLMError as e:
            logger.warning(f"Extraction LLM (URL) indisponible : {e}")
            # Dégradation : rien d'inventé, saisie manuelle possible côté /ajouter.
            return {"nom": "", "type_repas": "", "tags": [], "base": [],
                    "ingredients": scraped or [], "instructions": "",
                    "image_url": og_image, "source": "error"}
        type_repas = r.type_repas if r.type_repas in REPAS_OPTIONS else ""
        # Les ingrédients scrapés du HTML sont plus fiables que ceux du LLM.
        ingredients = scraped if scraped else r.ingredients
        return {
            "nom": clean_recipe_title(r.nom),
            "type_repas": type_repas,
            "tags": [t for t in r.tags if t in TAG_OPTIONS],
            "base": [b for b in r.base if b in BASE_OPTIONS],
            "ingredients": [str(i) for i in ingredients],
            "instructions": r.instructions,
            "image_url": og_image,
            "source": "llm",
        }

    async def extract_recipe_from_text(self, text: str) -> dict[str, Any]:
        """Structure une recette collée en texte libre (note perso, sortie
        Gemini/ChatGPT...) : titre, type, tags, ingrédients, instructions.

        Recopie sans inventer — utile quand il n'y a pas d'URL source."""
        raw_text = (text or "").strip()[:6000]
        if not raw_text:
            return {"nom": "", "type_repas": "", "tags": [], "base": [], "ingredients": [], "instructions": "", "source": "llm-text"}

        user_prompt = f"""Texte d'une recette (collé par l'utilisateur) :

{raw_text}

Structure-le sans rien inventer ni reformuler :
- nom : titre de la recette
- type_repas (cours du repas) : une valeur parmi {REPAS_OPTIONS}
- base (ingrédient principal) : 0 à 3 valeurs parmi {BASE_OPTIONS}
- tags (attributs) : 0 à 4 valeurs parmi {TAG_OPTIONS}
- ingredients : liste des ingrédients avec quantités, recopiés tels quels (un par entrée)
- instructions : étapes de cuisson recopiées textuellement, séparées par des retours à la ligne

Réponds UNIQUEMENT ce JSON :
{{"nom": "...", "type_repas": "...", "base": ["..."], "tags": ["..."], "ingredients": ["..."], "instructions": "..."}}"""

        try:
            r: RecipeExtraction = await self._complete(
                "", user_prompt, response_model=RecipeExtraction,
                label="text-extract", temperature=0.1, max_tokens=2000, core_field="nom",
            )
        except LLMError as e:
            logger.warning(f"Structuration LLM (texte) indisponible : {e}")
            return {"nom": "", "type_repas": "", "tags": [], "base": [], "ingredients": [],
                    "instructions": "", "image_url": "", "source": "error"}
        type_repas = r.type_repas if r.type_repas in REPAS_OPTIONS else ""
        return {
            "nom": clean_recipe_title(r.nom),
            "type_repas": type_repas,
            "tags": [t for t in r.tags if t in TAG_OPTIONS],
            "base": [b for b in r.base if b in BASE_OPTIONS],
            "ingredients": [str(i) for i in r.ingredients],
            "instructions": r.instructions,
            "image_url": "",
            "source": "llm-text",
        }
