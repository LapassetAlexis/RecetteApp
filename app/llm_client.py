"""Client Ollama — génération des plannings et extraction de recettes."""

import httpx
import json
from typing import Any

from app.config import settings

SYSTEM_PROMPT_PLANNING = """Tu es un chef cuisinier. Tu planifies des repas équilibrés.

RÈGLES :
- Choisis EXACTEMENT 8 recettes de la liste ci-dessous (4 jours × midi + soir).
- Équilibre viande/poisson/végé sur la semaine.
- Ne prends PAS de recettes listées comme "exclues".
- Tiens compte de la saison et de la température.

Répond UNIQUEMENT avec une liste numérotée comme ceci, SANS texte avant ni après :

1 - Jour 1 - midi - Nom exact de la recette
2 - Jour 1 - soir - Nom exact de la recette
3 - Jour 2 - midi - Nom exact de la recette
4 - Jour 2 - soir - Nom exact de la recette
5 - Jour 3 - midi - Nom exact de la recette
6 - Jour 3 - soir - Nom exact de la recette
7 - Jour 4 - midi - Nom exact de la recette
8 - Jour 4 - soir - Nom exact de la recette"""

SYSTEM_PROMPT_INGREDIENTS = """Tu es un assistant culinaire. Pour une recette donnée (nom + éventuellement URL), tu dois lister les ingrédients nécessaires.

RÈGLES :
- Sois précis et réaliste sur les quantités.
- Adapte les quantités au nombre de personnes indiqué.
- Regroupe les ingrédients similaires (ex: "oignons" même si utilisé plusieurs fois).

Retourne UNIQUEMENT un JSON valide avec cette structure :
{
  "ingredients": [
    {"nom": "nom ingrédient", "quantite": "quantité", "unite": "unité"},
    ...
  ],
  "cuisson_minutes": 30
}

Si l'URL n'est pas accessible, utilise tes connaissances culinaires pour estimer les ingrédients."""


class LLMClient:
    """Client pour interroger Ollama (modèle local)."""

    def __init__(self) -> None:
        self.url = f"{settings.ollama_url}/api/chat"
        self.model = settings.ollama_model

    async def _chat(
        self, system: str, user: str, temperature: float = 0.3
    ) -> str:
        """Envoie une requête de chat à Ollama et retourne le texte brut."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": temperature},
        }

        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.post(self.url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"]

    async def generate_planning(
        self,
        recettes: list[dict[str, Any]],
        saison: str,
        temperature: str,
        nb_personnes: int,
        ingredients_force: str,
        recettes_exclues: list[str],
    ) -> list[dict[str, Any]]:
        """Génère un planning de 4 jours via le LLM."""
        recettes_str = json.dumps(
            [
                {
                    "nom": r["nom"],
                    "type": r["repas"],
                    "tags": r["tags"],
                }
                for r in recettes
                if r["repas"] in ("Plat", "Entrée", "Légume", "Accompagnement", "")
            ],
            ensure_ascii=False,
            indent=2,
        )

        user_prompt = f"""Génère un planning de 4 jours pour une famille.

CONTEXTE :
- Saison : {saison}
- Température extérieure : {temperature}
- Nombre de personnes : {nb_personnes}
- In壓édients à forcer (restes) : {ingredients_force or "aucun"}
- Recettes déjà utilisées récemment (à exclure) : {', '.join(recettes_exclues) or 'aucune'}

BASE DE RECETTES DISPONIBLE :
{recettes_str}

Choisis exactement 8 recettes (4 jours × 2 repas). Équilibre les repas sur la semaine."""

        raw = await self._chat(SYSTEM_PROMPT_PLANNING, user_prompt, temperature=0.3)

        # Parsing de la réponse : liste numérotée ou JSON
        raw = raw.strip()
        import re

        plats = []

        # Essai 1 : détecter un pattern "N - Jour X - midi/soir - NomRecette"
        pattern = re.compile(
            r"(?:^|\n)\s*(?:\d+[\s\)\.]+)?(?:[\-\*]?\s*)?Jour\s*(\d+)\s*[\-\–]\s*(midi|soir|déjeuner|dîner)\s*[\-\–]\s*(.+?)(?=\n\s*(?:\d+[\s\)\.]+)?(?:[\-\*]?\s*)?Jour|\n*$)",
            re.IGNORECASE | re.DOTALL
        )
        matches = pattern.findall(raw)

        if matches:
            for match in matches:
                jour = int(match[0])
                moment = "midi" if match[1].lower() in ("midi", "déjeuner") else "soir"
                nom = match[2].strip().rstrip(")")
                # Nettoyer les éventuels tags entre parenthèses
                if "(" in nom:
                    nom = nom.split("(")[0].strip()
                plats.append({
                    "jour": jour,
                    "moment": moment,
                    "nom_recette": nom,
                    "type_repas": "Plat",
                    "raison": "",
                    "notion_id": "",
                    "url": "",
                    "notion_url": "",
                })

        # Essai 2 : format "1 - Jour 1 - midi - Nom"
        if not plats:
            pattern2 = re.compile(
                r"(?:\d+\s*[\-\–\)\.]\s*)?Jour\s*(\d+)\s*[\-\–]\s*(midi|soir|déjeuner|dîner)\s*[\-\–]\s*(.+)",
                re.IGNORECASE
            )
            matches2 = pattern2.findall(raw)
            for match in matches2:
                jour = int(match[0])
                moment = "midi" if match[1].lower() in ("midi", "déjeuner") else "soir"
                nom = match[2].strip().rstrip(")")
                if "(" in nom:
                    nom = nom.split("(")[0].strip()
                plats.append({
                    "jour": jour,
                    "moment": moment,
                    "nom_recette": nom,
                    "type_repas": "Plat",
                    "raison": "",
                    "notion_id": "",
                    "url": "",
                    "notion_url": "",
                })

        # Essai 3 : fallback JSON
        if not plats:
            try:
                data = json.loads(raw)
                return data.get("plats", [])
            except json.JSONDecodeError:
                match = re.search(r"\{.*\}", raw, re.DOTALL)
                if match:
                    data = json.loads(match.group())
                    return data.get("plats", [])

        # Essai 4 : lignes avec "midi" ou "soir" contenant un nom de recette
        if not plats:
            for line in raw.split("\n"):
                line = line.strip().strip("-* ")
                for moment_keyword in ["midi", "soir", "déjeuner", "dîner"]:
                    if moment_keyword in line.lower():
                        # Extraire le nom après "midi :" ou "soir :"
                        parts = re.split(rf"{moment_keyword}\s*[:\-–]\s*", line, flags=re.IGNORECASE)
                        if len(parts) > 1:
                            nom = parts[1].split("(")[0].strip()
                            if nom:
                                # Deviner le jour
                                jour_match = re.search(r"Jour\s*(\d+)", line, re.IGNORECASE)
                                jour_num = int(jour_match.group(1)) if jour_match else (len(plats) // 2 + 1)
                                moment_clean = "midi" if moment_keyword in ("midi", "déjeuner") else "soir"
                                plats.append({
                                    "jour": jour_num,
                                    "moment": moment_clean,
                                    "nom_recette": nom,
                                    "type_repas": "Plat",
                                    "raison": "",
                                    "notion_id": "",
                                    "url": "",
                                    "notion_url": "",
                                })
                                break

        if not plats:
            raise ValueError(
                f"Impossible de parser la réponse du LLM. Réponse reçue :\n{raw[:600]}"
            )

        return plats[:8]  # 8 max

    async def extract_ingredients(
        self,
        nom_recette: str,
        url: str = "",
        nb_personnes: int = 4,
    ) -> dict[str, Any]:
        """Extrait les ingrédients d'une recette via le LLM."""
        user_prompt = f"""Recette : {nom_recette}
URL : {url or "non fournie"}
Nombre de personnes : {nb_personnes}

Liste les ingrédients nécessaires pour préparer cette recette à {nb_personnes} personnes."""

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
            import re

            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                return json.loads(match.group())
            return {"ingredients": [], "cuisson_minutes": 30}

    async def extract_recipe_from_url(
        self, url: str
    ) -> dict[str, Any]:
        """Depuis une URL, extrait les infos de la recette pour créer la fiche Notion."""
        user_prompt = f"""Analyse l'URL suivante qui contient une recette de cuisine :
{url}

Extrais les informations suivantes :
- nom : le nom de la recette
- type_repas : le type (Plat, Dessert, Entrée, Goûter, Accompagnement, Apéro, Boisson, Petit dej)
- tags : une liste de tags pertinents (parmi : Viande, Poisson, Légumes, Soupe, Salade, Diet, Fun, Quiche/tarte, Tartines, Invités, Sur le pouce, Végétarien proténiné)

Retourne UNIQUEMENT ce JSON :
{{
  "nom": "...",
  "type_repas": "...",
  "tags": ["..."]
}}"""

        raw = await self._chat(
            "",
            user_prompt,
            temperature=0.1,
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
            import re

            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                return json.loads(match.group())
            return {"nom": "", "type_repas": "", "tags": []}
