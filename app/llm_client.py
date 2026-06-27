"""Client LLM — supporte Ollama (local) et Gemini (cloud)."""

import httpx
import json
import logging
import re
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_PLANNING = """Tu es un chef cuisinier. Tu planifies des repas équilibrés.

RÈGLES :
- Choisis EXACTEMENT 14 recettes de la liste ci-dessous (7 jours × midi + soir).
- Équilibre viande/poisson/végé sur la semaine.
- Ne prends PAS de recettes listées comme "exclues".
- Tiens compte de la saison et de la température.
- Varie les plats : pas deux fois la même recette.

Répond UNIQUEMENT avec une liste numérotée comme ceci, SANS texte avant ni après :

1 - Jour 1 - midi - Nom exact de la recette
2 - Jour 1 - soir - Nom exact de la recette
3 - Jour 2 - midi - Nom exact de la recette
4 - Jour 2 - soir - Nom exact de la recette
5 - Jour 3 - midi - Nom exact de la recette
6 - Jour 3 - soir - Nom exact de la recette
7 - Jour 4 - midi - Nom exact de la recette
8 - Jour 4 - soir - Nom exact de la recette
9 - Jour 5 - midi - Nom exact de la recette
10 - Jour 5 - soir - Nom exact de la recette
11 - Jour 6 - midi - Nom exact de la recette
12 - Jour 6 - soir - Nom exact de la recette
13 - Jour 7 - midi - Nom exact de la recette
14 - Jour 7 - soir - Nom exact de la recette"""

SYSTEM_PROMPT_INGREDIENTS = """Tu es un assistant culinaire. Pour une recette donnée (nom + éventuellement URL), liste les ingrédients nécessaires.

RÈGLES :
- Sois précis et réaliste sur les quantités.
- Adapte les quantités au nombre de personnes indiqué.
- Regroupe les ingrédients similaires (ex: "oignons" même si utilisé plusieurs fois).

Répond UNIQUEMENT avec ce JSON :
{"ingredients": [{"nom": "...", "quantite": "...", "unite": "..."}]}

Pas de texte avant ou après le JSON."""


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

    async def _chat(
        self, system: str, user: str, temperature: float = 0.3
    ) -> str:
        if self.provider == "gemini":
            try:
                return await self._chat_gemini(system, user, temperature)
            except Exception as e:
                logger.warning(f"⚠️ Gemini a échoué ({e}). Fallback vers Ollama...")
                return await self._chat_ollama(system, user, temperature)
        elif self.provider == "groq":
            try:
                return await self._chat_groq(system, user, temperature)
            except Exception as e:
                logger.warning(f"⚠️ Groq a échoué ({e}). Fallback vers Ollama...")
                return await self._chat_ollama(system, user, temperature)
        else:
            return await self._chat_ollama(system, user, temperature)

    async def _chat_ollama(
        self, system: str, user: str, temperature: float = 0.3
    ) -> str:
        url = f"{settings.ollama_url}/api/chat"
        payload = {
            "model": settings.ollama_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": temperature},
        }
        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"]

    async def _chat_groq(
        self, system: str, user: str, temperature: float = 0.3
    ) -> str:
        """Groq (API compatible OpenAI). Avec gestion rate limit."""
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": 2048,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(self._url, json=payload, headers=headers)
            if resp.status_code == 429:
                logger.warning("⚠️ Groq rate limit (429). Attente 5s puis retry...")
                import asyncio
                await asyncio.sleep(5)
                resp = await client.post(self._url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def _chat_gemini(
        self, system: str, user: str, temperature: float = 0.3
    ) -> str:
        payload = {
            "contents": [{"parts": [{"text": f"{system}\n\n{user}"}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": 2048,
            },
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(self._url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                raise ValueError("Gemini: aucune réponse")
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)

    async def _chat_ingredients_gemini(
        self, system: str, user: str, temperature: float = 0.1
    ) -> str:
        """Gemini avec contrainte JSON forcée."""
        payload = {
            "contents": [{"parts": [{"text": f"{system}\n\n{user}"}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": 1024,
            },
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(self._url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                raise ValueError("Gemini: aucune réponse")
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)

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
    ) -> list[dict[str, Any]]:
        recettes_str = json.dumps(
            [
                {"nom": r["nom"], "type": r["repas"], "tags": r["tags"]}
                for r in recettes
                if r["repas"] in ("Plat", "Entrée", "Légume", "Accompagnement", "")
            ],
            ensure_ascii=False,
            indent=2,
        )

        user_prompt = f"""Génère un planning de 7 jours pour une famille.

CONTEXTE :
- Saison : {saison}
- Température extérieure : {temperature}
- Nombre de personnes : {nb_personnes}
- Ingrédients à forcer (restes) : {ingredients_force or "aucun"}
- Recettes déjà utilisées récemment (à exclure) : {', '.join(recettes_exclues) or 'aucune'}

BASE DE RECETTES DISPONIBLE :
{recettes_str}

CONSIGNES SPÉCIFIQUES DE LA FAMILLE :
{custom_prompt or "Aucune consigne particulière."}

Choisis exactement 14 recettes (7 jours × 2 repas). Équilibre les repas sur la semaine."""

        raw = await self._chat(SYSTEM_PROMPT_PLANNING, user_prompt, temperature=0.3)
        return self._parse_planning(raw)

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
                        SYSTEM_PROMPT_INGREDIENTS, user_prompt, temperature=0.1
                    )
                else:
                    raw = await self._chat(
                        SYSTEM_PROMPT_INGREDIENTS, user_prompt, temperature=0.1
                    )
            except Exception as e:
                logger.warning(f"⚠️ {self.provider} ingrédients échoué ({e}). Fallback Ollama...")
                raw = await self._chat_ollama(
                    SYSTEM_PROMPT_INGREDIENTS, user_prompt, temperature=0.1
                )
        else:
            raw = await self._chat(
                SYSTEM_PROMPT_INGREDIENTS, user_prompt, temperature=0.1
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

    # ── Extraction depuis URL ────────────────────────────────────

    async def extract_recipe_from_url(
        self, url: str
    ) -> dict[str, Any]:
        user_prompt = f"""Analyse l'URL suivante qui contient une recette de cuisine :
{url}

Extrais les informations suivantes :
- nom : le nom de la recette
- type_repas : le type (Plat, Dessert, Entrée, Goûter, Accompagnement, Apéro, Boisson, Petit dej)
- tags : une liste de tags pertinents (parmi : Viande, Poisson, Légumes, Soupe, Salade, Diet, Fun, Quiche/tarte, Tartines, Invités, Sur le pouce, Végétarien proténiné)

Répond UNIQUEMENT ce JSON :
{{"nom": "...", "type_repas": "...", "tags": ["..."]}}"""

        raw = await self._chat("", user_prompt, temperature=0.1)
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
            return {"nom": "", "type_repas": "", "tags": []}
