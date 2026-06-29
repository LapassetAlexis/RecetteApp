"""Parseur Cooklang — transforme du texte cooklang en données structurées.

Format Cooklang :
>> Serves: 4
>> Time: 30m
>> Source: https://...

À @chauffer{1%cuillère à soupe} d'@huile d'olive dans une poêle.
Ajouter @oignons{2} émincés et cuire @temps{5%minutes}.
"""

import html
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CookIngredient:
    name: str
    quantity: str = ""
    unit: str = ""


@dataclass
class CookStep:
    text: str
    ingredients: list[CookIngredient] = field(default_factory=list)


@dataclass
class CookRecipe:
    title: str = ""
    serves: int = 4
    time: str = ""
    source: str = ""
    steps: list[CookStep] = field(default_factory=list)
    all_ingredients: list[CookIngredient] = field(default_factory=list)
    cookware: list[str] = field(default_factory=list)
    raw: str = ""


def parse(raw: str) -> CookRecipe:
    """Parse un texte Cooklang en structure de recette."""
    recipe = CookRecipe(raw=raw)
    lines = raw.split("\n")
    steps = []
    seen_ings: dict[str, CookIngredient] = {}

    # Métadonnées (lignes >>)
    meta_pattern = re.compile(r">>\s*(\w+)\s*:\s*(.+)")
    # Ingrédients @nom{quantité%unité}
    ing_pattern = re.compile(r"@(\w[\w\s\-'àâçéèêëîïôûù]*)")
    ing_full = re.compile(r"@(\w[\w\s\-'àâçéèêëîïôûù]*)\{([^}]*)\}")
    # Ustensiles #nom (pas les titres # avec espace)
    cook_pattern = re.compile(r"#([^\s][\w\s\-']*)")

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Métadonnées
        meta = meta_pattern.match(line)
        if meta:
            key = meta.group(1).lower()
            val = meta.group(2).strip()
            if key == "serves":
                try:
                    recipe.serves = int(val)
                except Exception: pass
            elif key == "time":
                recipe.time = val
            elif key == "source":
                recipe.source = val
            continue

        # Ligne de recette
        if line.startswith("@") or line.startswith("#") or re.match(r"^[\w\"']", line):
            step = CookStep(text=line)
            # Extraire les ingrédients
            for match in ing_full.finditer(line):
                name = match.group(1).strip()
                params = match.group(2)
                qty = ""
                unit = ""
                if params:
                    parts = params.split("%", 1)
                    qty = parts[0].strip()
                    if len(parts) > 1:
                        unit = parts[1].strip()
                ing = CookIngredient(name=name, quantity=qty, unit=unit)
                step.ingredients.append(ing)
                if name.lower() not in {k.lower() for k in seen_ings}:
                    seen_ings[name.lower()] = ing
            # Extraire les ustensiles
            for match in cook_pattern.finditer(line):
                cw = match.group(1).strip()
                if cw not in recipe.cookware:
                    recipe.cookware.append(cw)
            steps.append(step)

    recipe.steps = steps
    recipe.all_ingredients = list(seen_ings.values())
    return recipe


def to_html(recipe: CookRecipe, highlight_ings: list[str] | None = None) -> str:
    """Convertit une recette en HTML avec mise en valeur."""
    html_parts = []
    ings_hl = {i.lower() for i in (highlight_ings or [])}

    for step in recipe.steps:
        # Échapper le HTML d'abord (les noms/quantités viennent du LLM/Notion).
        # quote=False : on est dans du contenu texte, pas dans un attribut, donc
        # les apostrophes restent intactes (sinon les regex de noms cassent).
        text = html.escape(step.text, quote=False)
        # Remplacer @ingrédient{quantité%unité} par du HTML
        ing_full = re.compile(r"@(\w[\w\s\-'àâçéèêëîïôûù]*)\{([^}]*)\}")
        text = ing_full.sub(_ingredient_repl(ings_hl), text)
        # Remplacer #cookware par du HTML
        cook_pat = re.compile(r"#([^\s][\w\s\-']*)")
        text = cook_pat.sub(r'<span class="cook-cw">\1</span>', text)

        html_parts.append(f'<p class="cook-step">{text}</p>')

    return "\n".join(html_parts)


def _ingredient_repl(highlight_ings: set[str]):
    def repl(match):
        name = match.group(1).strip()
        params = match.group(2)
        qty = ""
        unit = ""
        if params:
            parts = params.split("%", 1)
            qty = parts[0].strip()
            if len(parts) > 1:
                unit = parts[1].strip()
        cls = "cook-ing"
        if name.lower() in highlight_ings:
            cls += " cook-ighl"
        label = name
        if qty:
            label = f"{qty} {unit} {name}" if unit else f"{qty}x {name}"
        return f'<span class="{cls}">{label}</span>'
    return repl
