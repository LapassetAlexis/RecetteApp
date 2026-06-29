"""Client LLM — supporte Ollama (local) et Gemini (cloud)."""

import asyncio
import html
import httpx
import json
import logging
import re
from typing import Any, Callable
from urllib.parse import urlparse

from app.config import REPAS_OPTIONS, TAG_OPTIONS, settings
from app.text_utils import clean_recipe_title

logger = logging.getLogger(__name__)

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
- Sois précis et réaliste sur les quantités.
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


def _flatten_instructions(val: Any) -> list[str]:
    """recipeInstructions : str | [str] | [HowToStep{text}] | [HowToSection{itemListElement}]."""
    steps: list[str] = []
    if isinstance(val, str):
        # parfois un seul bloc avec sauts de ligne
        for part in re.split(r"\n+|(?<=[.!?])\s{2,}", val):
            t = _clean_text(part)
            if t:
                steps.append(t)
    elif isinstance(val, list):
        for item in val:
            if isinstance(item, str):
                t = _clean_text(item)
                if t:
                    steps.append(t)
            elif isinstance(item, dict):
                if item.get("@type") == "HowToSection" and "itemListElement" in item:
                    steps.extend(_flatten_instructions(item["itemListElement"]))
                else:
                    t = _clean_text(item.get("text") or item.get("name") or "")
                    if t:
                        steps.append(t)
    return steps


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
            ingredients = [
                c for c in (_clean_text(i) for i in (node.get("recipeIngredient") or node.get("ingredients") or []))
                if c and c.lower() not in _NOISE
            ]
            instructions = _flatten_instructions(node.get("recipeInstructions", ""))
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
            }
    return None


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
        lignes = []
        for r in recettes:
            if r["repas"] not in ("Plat", "Entrée", "Légume", "Accompagnement", ""):
                continue
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
        user_prompt = f"""CONTEXTE : saison {saison}, température {temperature}, max {nb_personnes} pers.
Restes à écouler : {ingredients_force or "aucun"}.
Recettes à EXCLURE (déjà vues) : {exclues_str}.
Consignes famille : {custom_prompt or "aucune"}.

RECETTES DISPONIBLES (Nom | type | moment | tags) :
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
            for r in recettes:
                if r["nom"] not in valid and r["nom"] not in recettes_exclues:
                    valid.append(r["nom"])
                    if len(valid) >= n_needed:
                        break
        return self._assign_slots(valid[:n_needed], groups, unique_groups)

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
    ) -> list[dict[str, Any]]:
        """Construit les 14 créneaux : midis groupés = même plat, soirs tous
        différents (et différents des midis tant qu'il y a assez de recettes)."""
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
        return plats

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
        }

    # ── Extraction ingrédients ───────────────────────────────────

    async def extract_ingredients(
        self,
        nom_recette: str,
        url: str = "",
        nb_personnes: int = 4,
    ) -> dict[str, Any]:
        user_prompt = f"""Recette : {nom_recette}
URL : {url or "non fournie"}
Nombre de personnes : {nb_personnes}

Liste les ingrédients nécessaires pour préparer cette recette à {nb_personnes} personnes."""

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
    async def _classify_type_tags(
        self, nom: str, ingredients: list[str], keywords: list[str],
    ) -> tuple[str, list[str]]:
        """Choisit type_repas + tags parmi les valeurs Notion autorisées.

        Petit appel LLM (cheap) : ne touche PAS au contenu (nom/ingrédients/
        instructions restent ceux de la page), juste le classement.
        """
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
            tags = [t for t in data.get("tags", []) if t in TAG_OPTIONS]
            return type_repas, tags
        except Exception as e:
            logger.warning(f"Classification échouée ({e}), repli sur mots-clés")
            # Repli sans LLM : tags par correspondance avec les mots-clés
            kw_lower = {k.lower() for k in keywords}
            tags = [t for t in TAG_OPTIONS if t.lower() in kw_lower]
            return "", tags[:4]

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
        html_text = ""
        og_image = ""

        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
                resp.raise_for_status()
                html_text = resp.text
                og_match = re.search(
                    r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
                    html_text, re.IGNORECASE,
                )
                if og_match:
                    og_image = og_match.group(1)
        except Exception as e:
            logger.warning(f"Impossible de récupérer la page {url}: {e}")

        # 1) JSON-LD schema.org : données telles quelles, aucune déformation.
        ld = _extract_jsonld_recipe(html_text) if html_text else None
        if ld and ld.get("ingredients"):
            logger.info("Recette extraite via JSON-LD (exact, sans LLM)")
            type_repas, tags = await self._classify_type_tags(
                ld["nom"], ld["ingredients"], ld.get("keywords", []),
            )
            return {
                "nom": ld["nom"],
                "type_repas": type_repas,
                "tags": tags,
                "ingredients": ld["ingredients"],          # list[str], lignes brutes
                "instructions": ld["instructions"],
                "image_url": ld.get("image_url") or og_image,
                "source": "jsonld",
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
        return {
            "nom": clean_recipe_title(data.get("nom", "")),
            "type_repas": type_repas,
            "tags": [t for t in data.get("tags", []) if t in TAG_OPTIONS],
            "ingredients": [str(i) for i in ingredients],
            "instructions": data.get("instructions", ""),
            "image_url": og_image,
            "source": "llm",
        }
