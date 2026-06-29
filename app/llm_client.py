"""Client LLM — supporte Ollama (local) et Gemini (cloud)."""

import asyncio
import httpx
import json
import logging
import re
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_PLANNING = """Tu es un chef. Tu planifies des repas équilibrés.

RÈGLES :
- Remplis EXACTEMENT 14 créneaux (Jour 1 à 7, midi + soir).
- Ne répète jamais une recette, SAUF si la "RÉPARTITION DES MIDIS" demande le même plat sur des jours groupés (recopie alors le même nom).
- Équilibre viande/poisson/végé. Ignore les recettes "exclues". Tiens compte de la saison/température.

Réponds UNIQUEMENT par 14 lignes au format exact, rien d'autre :
N - Jour X - midi|soir - Nom exact de la recette"""

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

        # Construire la description des groupes de midis
        jours = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
        groups = [int(x) for x in midi_groups.split(",")]
        by_group: dict[int, list[int]] = {}
        for i, g in enumerate(groups):
            by_group.setdefault(g, []).append(i)
        midi_lines = []
        for g in sorted(by_group):
            idxs = by_group[g]
            names = [jours[i] for i in idxs]
            count = len(idxs)
            if count == 1:
                midi_lines.append(f"- {names[0]} midi : plat différent")
            else:
                midi_lines.append(f"- {' + '.join(names)} midi : même plat (cuisiné pour {count})")
        midi_desc = "\n".join(midi_lines)

        exclues_str = ', '.join(recettes_exclues) or 'aucune'
        user_prompt = f"""CONTEXTE : saison {saison}, température {temperature}, max {nb_personnes} pers.
Restes à écouler : {ingredients_force or "aucun"}.
Recettes à EXCLURE (déjà vues) : {exclues_str}.
Consignes famille : {custom_prompt or "aucune"}.

RECETTES DISPONIBLES (Nom | type | moment | tags) :
{recettes_str}

RÉPARTITION DES MIDIS :
{midi_desc}
- Soirs : tous différents, légers/rapides (un soir = restes).

Donne les 14 lignes."""

        raw = await self._chat(
            SYSTEM_PROMPT_PLANNING, user_prompt,
            temperature=0.3, max_tokens=600, label="planning",
        )
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

    # ── Extraction depuis URL ────────────────────────────────────
    async def extract_recipe_from_url(
        self, url: str
    ) -> dict[str, Any]:
        """Récupère le contenu réel de l'URL et extrait les infos via LLM."""
        page_text = ""
        og_image = ""

        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
                resp.raise_for_status()
                html = resp.text

                # Extraire l'image og:image
                og_match = re.search(
                    r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
                    html, re.IGNORECASE
                )
                if og_match:
                    og_image = og_match.group(1)

                # Extraire le texte visible (stripper les balises)
                text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r'<[^>]+>', ' ', text)
                text = re.sub(r'\s+', ' ', text).strip()
                # 2500 caractères suffisent pour nom + étapes ; au-delà c'est
                # surtout du boilerplate (commentaires, suggestions) = tokens perdus.
                page_text = text[:2500]

        except Exception as e:
            logger.warning(f"Impossible de récupérer la page {url}: {e}")

        user_prompt = f"""Voici le contenu textuel d'une page web de recette de cuisine.

CONTENU DE LA PAGE :
{page_text or "Contenu non accessible, utilise tes connaissances."}

À partir de ce contenu, extrais EXACTEMENT les informations suivantes :
- nom : le nom de la recette (copie-le tel quel depuis le texte)
- type_repas : le type (Plat, Dessert, Entrée, Goûter, Accompagnement, Apéro, Petit dej)
- tags : une liste de tags parmi : Viande, Poisson, Légumes, Soupe, Salade, Diet, Fun, Quiche/tarte, Tartines, Invités, Sur le pouce, Végétarien proténiné
- instructions : RECOPIE TEXTUELLEMENT les étapes de cuisson, sans reformuler, sans ajouter d'introduction, séparées par des retours à la ligne
- image_url : "{og_image}" (utilise cette URL si elle existe, sinon laisse vide)

Répond UNIQUEMENT ce JSON :
{{"nom": "...", "type_repas": "...", "tags": ["..."], "instructions": "...", "image_url": "{og_image}"}}"""

        raw = await self._chat("", user_prompt, temperature=0.1, max_tokens=1500, label="url-extract", json_mode=True)
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
