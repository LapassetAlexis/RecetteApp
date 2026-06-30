"""Client LLM — supporte Ollama (local) et Gemini (cloud)."""

import asyncio
import html
import httpx
import ipaddress
import json
import logging
import re
import socket
import unicodedata
from typing import Any, Callable
from urllib.parse import urlparse

from app.config import REPAS_OPTIONS, TAG_OPTIONS, settings
from app.text_utils import clean_recipe_title

logger = logging.getLogger(__name__)

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

SYSTEM_PROMPT_PLANNING = """Tu es un chef. Tu SÉLECTIONNES des recettes variées pour une semaine.

RÈGLES :
- Choisis le nombre EXACT de recettes demandé, TOUTES DIFFÉRENTES les unes des autres.
- Uniquement des recettes présentes dans la liste fournie. N'invente JAMAIS un plat.
- VARIÉTÉ MAXIMALE : pioche dans toute la liste. Alterne les protéines (viande / poisson / végé / œufs) et les styles (mijoté, four, poêle, cru, soupe...).
- N'utilise PAS les recettes de la liste "exclues". Tiens compte de la saison et de la température.

L'organisation midi/soir est gérée ensuite par l'application : fournis seulement la sélection.

Réponds UNIQUEMENT par une ligne par recette, au format exact, rien d'autre :
N - Nom exact de la recette"""

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

    async def _chat(
        self, system: str, user: str, temperature: float = 0.3,
        max_tokens: int = 2048, label: str = "chat", json_mode: bool = False,
    ) -> str:
        if self.provider == "gemini":
            try:
                return await self._chat_gemini(system, user, temperature, max_tokens, label, json_mode)
            except Exception as e:
                logger.warning(f"⚠️ Gemini a échoué ({e}). Fallback vers Ollama...")
                return await self._chat_ollama(system, user, temperature, max_tokens, json_mode)
        elif self.provider == "groq":
            try:
                return await self._chat_groq(system, user, temperature, max_tokens, label, json_mode)
            except Exception as e:
                logger.warning(f"⚠️ Groq a échoué ({e}). Fallback vers Ollama...")
                return await self._chat_ollama(system, user, temperature, max_tokens, json_mode)
        else:
            return await self._chat_ollama(system, user, temperature, max_tokens, json_mode)

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
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(self._url, json=payload, headers=headers)
            if resp.status_code == 429:
                logger.warning("⚠️ Groq rate limit (429). Attente 5s puis retry...")
                await asyncio.sleep(5)
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

    async def _chat_ingredients_gemini(
        self, system: str, user: str, temperature: float = 0.1, max_tokens: int = 1024
    ) -> str:
        """Gemini avec contrainte JSON forcée."""
        return await self._chat_gemini(system, user, temperature, max_tokens, "ingredients", json_mode=True)

    # ── Génération planning ──────────────────────────────────────

    @staticmethod
    def _is_side(r: dict[str, Any]) -> bool:
        """Recette utilisable comme accompagnement (légume / garniture)."""
        return r.get("repas") in ("Légume", "Accompagnement")

    @staticmethod
    def _is_complete(r: dict[str, Any]) -> bool:
        """Plat complet : type « Plat » ou tag « plat ». Se suffit à lui-même,
        donc pas d'accompagnement."""
        if r.get("repas") == "Plat":
            return True
        return any(str(t).strip().lower() == "plat" for t in (r.get("tags") or []))

    @classmethod
    def _needs_side(cls, r: dict[str, Any]) -> bool:
        """On propose un accompagnement par défaut aux plats principaux
        (l'utilisateur peut le vider/changer). Pas d'accompagnement pour un
        accompagnement lui-même, ni pour un plat complet (type/tag « plat »)."""
        if cls._is_side(r) or cls._is_complete(r):
            return False
        return True

    # Tags de saison normalisés (sans accents) — dérivé de config.SAISON_TAGS.
    _SAISONS = {"printemps", "ete", "automne", "hiver"}

    @staticmethod
    def _norm(s: Any) -> str:
        """Minuscule sans accents (« Été » -> « ete »)."""
        s = unicodedata.normalize("NFD", str(s or "").strip().lower())
        return "".join(c for c in s if unicodedata.category(c) != "Mn")

    @classmethod
    def _recipe_seasons(cls, r: dict[str, Any]) -> set[str]:
        """Tags de saison portés par une recette (vide = toutes saisons)."""
        return {cls._norm(t) for t in (r.get("tags") or [])} & cls._SAISONS

    @classmethod
    def _season_rank(cls, r: dict[str, Any], saison_n: str) -> int:
        """0 = saison demandée explicitement, 1 = toutes saisons (neutre),
        2 = uniquement une AUTRE saison (à éviter)."""
        secs = cls._recipe_seasons(r)
        if not secs:
            return 1
        return 0 if saison_n in secs else 2

    async def generate_planning(
        self,
        recettes: list[dict[str, Any]],
        saison: str,
        temperature: str,
        nb_personnes: int,
        ingredients_force: str,
        recettes_exclues: list[str],
        custom_prompt: str = "",
        midi_groups: str = "1,1,2,2,2,3,4",
        per_day: str = "2,2,2,2,2,4,4",
    ) -> list[dict[str, Any]]:
        # Liste compacte (1 ligne/recette) plutôt que du JSON indenté :
        # beaucoup moins de tokens, lisible par le modèle.
        # Format : Nom | type | moment | tags
        # Le LLM choisit des PLATS principaux ; les accompagnements (légumes /
        # garnitures) sont appariés ensuite par le code aux plats qui en ont
        # besoin (viande/poisson nature). On ne propose donc au LLM que les plats.
        sides = [r for r in recettes if self._is_side(r)]
        meta = {r["nom"].lower().strip(): r for r in recettes}

        # Filtrage/pondération par saison : on écarte les plats tagués pour une
        # AUTRE saison (rank 2), et on présente d'abord ceux de la saison
        # demandée (rank 0), puis les « toutes saisons » (rank 1). Les recettes
        # sans tag de saison restent toujours éligibles.
        saison_n = self._norm(saison)
        mains = [r for r in recettes
                 if not self._is_side(r) and r["repas"] in ("Plat", "Entrée", "")]
        eligibles = sorted(
            (r for r in mains if self._season_rank(r, saison_n) < 2),
            key=lambda r: self._season_rank(r, saison_n),
        )

        lignes = []
        for r in eligibles:
            parts = [r["nom"]]
            if r["repas"]:
                parts.append(r["repas"])
            if r.get("moment"):
                parts.append(r["moment"])
            if r["tags"]:
                parts.append(",".join(r["tags"]))
            lignes.append(" | ".join(parts))
        recettes_str = "\n".join(lignes)

        # Groupes de midis (ex. [1,1,2,2,2,3,4]). On en déduit le nombre de
        # midis DISTINCTS à choisir + 7 soirs tous différents.
        try:
            groups = [int(x) for x in midi_groups.split(",")]
        except (ValueError, TypeError):
            groups = [1, 1, 2, 2, 2, 3, 4]
        if len(groups) != 7:
            groups = [1, 1, 2, 2, 2, 3, 4]
        unique_groups: list[int] = []
        for g in groups:
            if g not in unique_groups:
                unique_groups.append(g)
        n_needed = len(unique_groups) + 7  # midis distincts + 7 soirs

        exclues_str = ', '.join(recettes_exclues) or 'aucune'
        user_prompt = f"""CONTEXTE — SAISON : {saison}. TEMPÉRATURE : {temperature}. Max {nb_personnes} pers.
Restes à écouler : {ingredients_force or "aucun"}.
Recettes à EXCLURE (déjà vues) : {exclues_str}.
Consignes famille : {custom_prompt or "aucune"}.

PRIORITÉ SAISON/TEMPÉRATURE :
- Privilégie des plats cohérents avec la saison « {saison} » et une météo « {temperature} ».
- Temps frais/froid → plats chauds, mijotés, soupes, gratins. Temps chaud → salades, plats froids, grillades légères.
- La liste est déjà triée : les recettes en tête conviennent le mieux à la saison. Pioche en priorité dedans.

RECETTES DISPONIBLES (Nom | type | moment | tags) — triées par pertinence saison :
{recettes_str}

Choisis EXACTEMENT {n_needed} recettes DIFFÉRENTES et variées de cette liste.
Donne {n_needed} lignes, une par recette, au format « N - Nom exact »."""

        raw = await self._chat(
            SYSTEM_PROMPT_PLANNING, user_prompt,
            temperature=0.5, max_tokens=600, label="planning",
        )
        names = self._parse_recipe_names(raw)

        # Ne garder que des noms RÉELLEMENT présents dans la base (le nom exact
        # Notion), et compléter si le LLM en a donné trop peu.
        lookup = {r["nom"].lower().strip(): r["nom"] for r in recettes}
        valid: list[str] = []
        for n in names:
            canon = lookup.get(n.lower().strip())
            if canon and canon not in valid:
                valid.append(canon)
        if len(valid) < n_needed:
            # Complète d'abord avec les plats éligibles à la saison (déjà triés),
            # puis, en dernier recours, avec n'importe quel autre plat.
            others = [r for r in mains if self._season_rank(r, saison_n) >= 2]
            for r in eligibles + others:
                if r["nom"] not in valid and r["nom"] not in recettes_exclues:
                    valid.append(r["nom"])
                    if len(valid) >= n_needed:
                        break
        return self._assign_slots(valid[:n_needed], groups, unique_groups, meta, sides)

    def _parse_recipe_names(self, raw: str) -> list[str]:
        """Extrait une liste de noms de recettes d'une réponse LLM (liste numérotée)."""
        names: list[str] = []
        for line in raw.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^\s*\d+\s*[\-\.\)–]\s*(.+)$", line)
            cand = m.group(1) if m else re.sub(r"^[\-\*•]\s*", "", line)
            cand = cand.strip().strip('"').strip()
            cand = re.sub(r"\s*\(.*\)\s*$", "", cand).strip()  # retirer "(dessert)" etc.
            if cand:
                names.append(cand)
        # dédup en gardant l'ordre
        seen, out = set(), []
        for n in names:
            k = n.lower()
            if k not in seen:
                seen.add(k)
                out.append(n)
        return out

    def _assign_slots(
        self, names: list[str], groups: list[int], unique_groups: list[int],
        meta: dict[str, Any] | None = None, sides: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Construit les 14 créneaux : midis groupés = même plat, soirs tous
        différents (et différents des midis tant qu'il y a assez de recettes).
        Les plats « protéine nature » reçoivent un accompagnement (légume)."""
        if not names:
            return []
        n_groups = len(unique_groups)
        midi_by_group = {g: names[i % len(names)] for i, g in enumerate(unique_groups)}
        soirs = names[n_groups:]

        plats: list[dict[str, Any]] = []
        for day in range(1, 8):
            plats.append(self._make_plat(day, "midi", midi_by_group[groups[day - 1]]))
        for day in range(1, 8):
            idx = day - 1
            nom = soirs[idx] if idx < len(soirs) else names[(n_groups + idx) % len(names)]
            plats.append(self._make_plat(day, "soir", nom))

        self._attach_sides(plats, meta or {}, sides or [])
        return plats

    def _attach_sides(
        self, plats: list[dict[str, Any]],
        meta: dict[str, Any], sides: list[dict[str, Any]],
    ) -> None:
        """Apparie un légume/accompagnement aux plats viande/poisson nature.
        Tourne dans la liste des accompagnements pour varier sur la semaine."""
        if not sides:
            return
        # Accompagnement attribué PAR NOM de plat : un même plat sur plusieurs
        # jours consécutifs reçoit le même accompagnement, ce qui préserve la
        # fusion des cases du planning (clé de fusion = nom + accompagnement).
        assigned: dict[str, dict] = {}
        k = 0
        for plat in plats:
            nom_key = plat["nom_recette"].lower().strip()
            m = meta.get(nom_key)
            if not (m and self._needs_side(m)):
                continue
            if nom_key not in assigned:
                s = sides[k % len(sides)]
                k += 1
                assigned[nom_key] = {
                    "nom_recette": s["nom"],
                    "notion_id": s.get("id", ""),
                    "url": s.get("url", ""),
                    "notion_url": s.get("notion_url", ""),
                }
            plat["accompagnement"] = assigned[nom_key]

    def _parse_planning(self, raw: str) -> list[dict[str, Any]]:
        """Parse la réponse du LLM (liste numérotée, markdown ou JSON)."""
        raw = raw.strip()
        plats = []

        # Essai 1 : liste numérotée "N - Jour X - midi - Nom"
        pattern = re.compile(
            r"(?:^|\n)\s*(?:\d+[\s\)\.]+)?(?:[\-\*]?\s*)?Jour\s*(\d+)\s*[\-\–]\s*(midi|soir|déjeuner|dîner)\s*[\-\–]\s*(.+?)(?=\n\s*(?:\d+[\s\)\.]+)?(?:[\-\*]?\s*)?Jour|\n*$)",
            re.IGNORECASE | re.DOTALL
        )
        matches = pattern.findall(raw)
        if matches:
            for match in matches:
                nom = match[2].strip().rstrip(")")
                if "(" in nom:
                    nom = nom.split("(")[0].strip()
                plats.append(self._make_plat(int(match[0]), match[1], nom))

        # Essai 2 : "Jour X - midi - Nom" sans numéro
        if not plats:
            pattern2 = re.compile(
                r"Jour\s*(\d+)\s*[\-\–]\s*(midi|soir|déjeuner|dîner)\s*[\-\–]\s*(.+)",
                re.IGNORECASE
            )
            for match in pattern2.findall(raw):
                nom = match[2].strip().rstrip(")")
                if "(" in nom:
                    nom = nom.split("(")[0].strip()
                plats.append(self._make_plat(int(match[0]), match[1], nom))

        # Essai 3 : markdown "**Jour N - moment**" + puces
        if not plats:
            lines = raw.split("\n")
            for i, line in enumerate(lines):
                header_match = re.search(
                    r"\*\*\s*Jour\s*(\d+)\s*[\-\–]\s*(midi|soir|déjeuner|dîner)\s*\*\*",
                    line, re.IGNORECASE
                )
                if header_match:
                    jour = int(header_match.group(1))
                    moment = "midi" if header_match.group(2).lower() in ("midi", "déjeuner") else "soir"
                    for j in range(i + 1, min(i + 5, len(lines))):
                        bullet = lines[j].strip().lstrip("*-•").strip()
                        if bullet and not bullet.startswith("**") and not bullet.startswith("#"):
                            if "(" in bullet:
                                bullet = bullet.split("(")[0].strip()
                            if bullet:
                                plats.append(self._make_plat(jour, moment, bullet))
                            break

        # Essai 4 : JSON
        if not plats:
            try:
                data = json.loads(raw)
                return data.get("plats", [])
            except json.JSONDecodeError:
                match = re.search(r"\{.*\}", raw, re.DOTALL)
                if match:
                    data = json.loads(match.group())
                    return data.get("plats", [])

        # Essai 5 : lignes libres avec "midi/soir : Nom"
        if not plats:
            for line in raw.split("\n"):
                line = line.strip().strip("-* ")
                for kw in ["midi", "soir", "déjeuner", "dîner"]:
                    if kw in line.lower():
                        parts = re.split(rf"{kw}\s*[::\-]\s*", line, flags=re.IGNORECASE)
                        if len(parts) > 1:
                            nom = parts[1].split("(")[0].strip()
                            if nom:
                                jour_m = re.search(r"Jour\s*(\d+)", line, re.IGNORECASE)
                                jour_n = int(jour_m.group(1)) if jour_m else (len(plats) // 2 + 1)
                                m = "midi" if kw in ("midi", "déjeuner") else "soir"
                                plats.append(self._make_plat(jour_n, m, nom))
                                break

        if not plats:
            raise ValueError(
                f"Impossible de parser la réponse du LLM.\n{raw[:600]}"
            )

        return plats[:14]

    def _make_plat(self, jour: int, moment: str, nom: str) -> dict:
        return {
            "jour": jour,
            "moment": "midi" if moment.lower() in ("midi", "déjeuner") else "soir",
            "nom_recette": nom.strip(),
            "type_repas": "Plat",
            "raison": "",
            "notion_id": "",
            "url": "",
            "notion_url": "",
            "accompagnement": None,
        }

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

        if self.provider in ("gemini", "groq"):
            try:
                if self.provider == "gemini":
                    raw = await self._chat_ingredients_gemini(
                        SYSTEM_PROMPT_INGREDIENTS, user_prompt, temperature=0.1, max_tokens=512
                    )
                else:
                    raw = await self._chat(
                        SYSTEM_PROMPT_INGREDIENTS, user_prompt,
                        temperature=0.1, max_tokens=512, label="ingredients", json_mode=True,
                    )
            except Exception as e:
                logger.warning(f"⚠️ {self.provider} ingrédients échoué ({e}). Fallback Ollama...")
                raw = await self._chat_ollama(
                    SYSTEM_PROMPT_INGREDIENTS, user_prompt, temperature=0.1, max_tokens=512, json_mode=True
                )
        else:
            raw = await self._chat(
                SYSTEM_PROMPT_INGREDIENTS, user_prompt,
                temperature=0.1, max_tokens=512, label="ingredients", json_mode=True,
            )

        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                return json.loads(match.group())
            return {"ingredients": [], "cuisson_minutes": 30}

    async def batch_extract_ingredients(
        self,
        plats: list[dict[str, Any]],
        nb_personnes: int,
    ) -> list[dict[str, Any]]:
        """Extrait et déduplonne les ingrédients de TOUTES les recettes en 1 appel."""
        recettes_str = "\n".join(
            f"{i+1}. {p['nom_recette']} ({p.get('moment', '?')})"
            for i, p in enumerate(plats)
        )

        user_prompt = f"""Voici 14 recettes pour une semaine à {nb_personnes} personnes :

{recettes_str}

Pour chaque recette, liste les ingrédients nécessaires, PUIS regroupe-les en UNE SEULE liste sans doublon.

Exemple : si "huile d'olive" apparaît dans 3 recettes, additionne les quantités.

Répond UNIQUEMENT ce JSON :
{{"ingredients": [
  {{"nom": "huile d'olive", "quantite": "6", "unite": "cuillères à soupe"}},
  {{"nom": "oignons", "quantite": "4", "unite": "pièces"}}
]}}"""

        try:
            if self.provider == "gemini":
                raw = await self._chat_ingredients_gemini(
                    "", user_prompt, temperature=0.1, max_tokens=1024
                )
            else:
                raw = await self._chat(
                    "", user_prompt, temperature=0.1, max_tokens=1024, label="batch-ing", json_mode=True,
                )
        except Exception as e:
            logger.warning(f"⚠️ Batch ingrédients échoué ({e}), fallback extraction individuelle...")
            return []

        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()

        try:
            data = json.loads(raw)
            return data.get("ingredients", [])
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                data = json.loads(match.group())
                return data.get("ingredients", [])
            logger.warning("Batch ingrédients : JSON invalide, fallback extraction individuelle")
            return []

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

    def _keyword_guess(
        self, nom: str, ingredients: list[str], keywords: list[str],
    ) -> tuple[str, list[str], bool, bool]:
        """Heuristique sans LLM : (type_repas deviné, tags par mots-clés,
        savory?, sweet?). Sert de repli ET de garde-fou contre le LLM."""
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
        return t, tags, savory, sweet

    async def _classify_type_tags(
        self, nom: str, ingredients: list[str], keywords: list[str],
    ) -> tuple[str, list[str]]:
        """Choisit type_repas + tags parmi les valeurs Notion autorisées.

        Petit appel LLM (cheap) : ne touche PAS au contenu (nom/ingrédients/
        instructions restent ceux de la page), juste le classement. Un garde-fou
        lexical corrige les erreurs flagrantes (ex. cheesecake salé → pas Dessert)
        et complète les tags manquants."""
        guess_type, kw_tags, savory, sweet = self._keyword_guess(nom, ingredients, keywords)
        ing_sample = ", ".join(ingredients[:12])
        prompt = f"""Recette : "{nom}"
Ingrédients : {ing_sample or "inconnus"}
Mots-clés du site : {", ".join(keywords) or "aucun"}

Classe cette recette :
- type_repas : EXACTEMENT une valeur parmi {REPAS_OPTIONS}
- tags : 0 à 4 valeurs parmi {TAG_OPTIONS}

Réponds UNIQUEMENT ce JSON : {{"type_repas": "...", "tags": ["..."]}}"""
        try:
            raw = await self._chat(
                "", prompt, temperature=0.0, max_tokens=150,
                label="classify", json_mode=True,
            )
            data = self._parse_json(raw)
            type_repas = data.get("type_repas", "")
            if type_repas not in REPAS_OPTIONS:
                type_repas = ""
            # Garde-fou : un plat clairement salé ne peut pas être un Dessert.
            if type_repas == "Dessert" and savory and not sweet:
                logger.info(f"Classification corrigée : '{nom}' Dessert → {guess_type} (salé)")
                type_repas = guess_type
            if not type_repas:
                type_repas = guess_type
            # Union tags LLM + tags mots-clés (sans doublon, max 4).
            tags = [t for t in data.get("tags", []) if t in TAG_OPTIONS]
            for t in kw_tags:
                if t not in tags:
                    tags.append(t)
            return type_repas, tags[:4]
        except Exception as e:
            logger.warning(f"Classification échouée ({e}), repli sur mots-clés")
            return guess_type, kw_tags[:4]

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
                return json.loads(match.group())
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
            type_repas, tags = await self._classify_type_tags(
                ld["nom"], ld_ings, ld.get("keywords", []),
            )
            return {
                "nom": ld["nom"],
                "type_repas": type_repas,
                "tags": tags,
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
                type_repas, tags = await self._classify_type_tags(
                    h.get("nom", ""), h["ingredients"], h.get("keywords", []),
                )
                return {
                    "nom": h.get("nom", ""),
                    "type_repas": type_repas,
                    "tags": tags,
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
- type_repas : une valeur parmi {REPAS_OPTIONS}
- tags : 0 à 4 valeurs parmi {TAG_OPTIONS}
- ingredients : liste des ingrédients avec quantités, recopiés tels quels (un par entrée)
- instructions : étapes de cuisson recopiées textuellement, séparées par des retours à la ligne

Réponds UNIQUEMENT ce JSON :
{{"nom": "...", "type_repas": "...", "tags": ["..."], "ingredients": ["..."], "instructions": "..."}}"""

        raw = await self._chat(
            "", user_prompt, temperature=0.1, max_tokens=2000,
            label="url-extract", json_mode=True,
        )
        data = self._parse_json(raw)
        # Normaliser / valider
        type_repas = data.get("type_repas", "")
        if type_repas not in REPAS_OPTIONS:
            type_repas = ""
        ingredients = data.get("ingredients", [])
        if isinstance(ingredients, str):
            ingredients = [l.strip() for l in ingredients.split("\n") if l.strip()]
        # Les ingrédients scrapés du HTML sont plus fiables que ceux du LLM.
        if scraped:
            ingredients = scraped
        # Le LLM renvoie parfois instructions en liste : on garde le contrat string.
        instructions = data.get("instructions", "")
        if isinstance(instructions, list):
            instructions = "\n".join(str(s).strip() for s in instructions if str(s).strip())
        return {
            "nom": clean_recipe_title(data.get("nom", "")),
            "type_repas": type_repas,
            "tags": [t for t in data.get("tags", []) if t in TAG_OPTIONS],
            "ingredients": [str(i) for i in ingredients],
            "instructions": instructions,
            "image_url": og_image,
            "source": "llm",
        }
