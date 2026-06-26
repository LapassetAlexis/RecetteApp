"""Client Ollama — génération des plannings et extraction de recettes."""

import httpx
import json
from typing import Any

from app.config import settings

SYSTEM_PROMPT_PLANNING = """Tu es un chef cuisinier spécialisé dans la planification de repas équilibrés pour une famille française.

Tu travailles avec une base de données de recettes Notion. Pour chaque recette, tu as : le nom, le type de repas (Plat, Dessert, Entrée, etc.), des tags (Viande, Poisson, Légumes, etc.), et parfois une URL.

RÈGLES STRICTES :
1. Choisis EXACTEMENT 8 recettes (4 jours × 2 repas : midi et soir).
2. Chaque jour doit avoir un midi et un soir cohérents (pas deux fois le même plat).
3. Équilibre sur les 4 jours : varier viande/poisson/végé, pas de répétition.
4. Privilégie les recettes étiquetées "Plat" pour les plats principaux.
5. Tiens compte de la saison et de la température donnée.
6. Ne JAMAIS sélectionner une recette de la liste "exclues".
7. Si des ingrédients à forcer sont donnés, privilégie les recettes qui les contiennent.

Retourne UNIQUEMENT un JSON valide avec cette structure exacte, sans texte autour :
{
  "plats": [
    {
      "jour": 1,
      "moment": "midi",
      "nom_recette": "...",
      "type_repas": "Plat",
      "raison": "..."
    },
    ...
  ]
}

Pour chaque recette, ajoute une "raison" très courte (10-15 mots max) expliquant pourquoi tu l'as choisie."""

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

        # Nettoyage et parsing du JSON
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()

        try:
            data = json.loads(raw)
            return data.get("plats", [])
        except json.JSONDecodeError:
            # Fallback : tenter d'extraire un objet JSON
            import re

            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                data = json.loads(match.group())
                return data.get("plats", [])
            raise ValueError(f"Impossible de parser la réponse du LLM:\n{raw[:500]}")

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
