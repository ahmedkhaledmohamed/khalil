"""Recipe finder — search recipes by ingredient, cuisine, or name.

Uses TheMealDB free API (no key required) and local caching.
"""

import asyncio
import json
import logging
import re
from pathlib import Path
from urllib.request import urlopen

from config import DATA_DIR

log = logging.getLogger("khalil.actions.recipe_finder")

SKILL = {
    "name": "recipe_finder",
    "description": "Find recipes by ingredient, cuisine, or name",
    "category": "lifestyle",
    "patterns": [
        (r"\brecipe\s+(?:for|with|using)\b", "recipe_search"),
        (r"\bfind\s+(?:a\s+)?recipe\b", "recipe_search"),
        (r"\bwhat\s+can\s+I\s+(?:make|cook)\s+with\b", "recipe_by_ingredient"),
        (r"\brecipes?\s+with\s+", "recipe_by_ingredient"),
        (r"\bcook\s+(?:me\s+)?(?:something|a)\b", "recipe_random"),
        (r"\brandom\s+recipe\b", "recipe_random"),
        (r"\bwhat\s+should\s+I\s+(?:cook|eat|make)\b", "recipe_random"),
        (r"\bmeal\s+(?:idea|suggestion|inspiration)\b", "recipe_random"),
    ],
    "actions": [
        {"type": "recipe_search", "handler": "handle_intent", "keywords": "recipe find search cook meal food", "description": "Search recipes by name"},
        {"type": "recipe_by_ingredient", "handler": "handle_intent", "keywords": "recipe ingredient cook make with using", "description": "Find recipes by ingredient"},
        {"type": "recipe_random", "handler": "handle_intent", "keywords": "recipe random suggestion idea what cook eat meal", "description": "Get a random recipe suggestion"},
    ],
    "examples": [
        "Find a recipe for pasta",
        "What can I make with chicken and rice?",
        "What should I cook tonight?",
        "Recipe with avocado",
    ],
}

_BASE_URL = "https://www.themealdb.com/api/json/v1/1"


async def _fetch_json(url: str) -> dict | None:
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: urlopen(url, timeout=10).read())
        return json.loads(response)
    except Exception as e:
        log.warning("MealDB API error: %s", e)
        return None


def _format_meal(meal: dict) -> str:
    name = meal.get("strMeal", "Unknown")
    category = meal.get("strCategory", "")
    area = meal.get("strArea", "")
    instructions = meal.get("strInstructions", "")

    # Collect ingredients
    ingredients = []
    for i in range(1, 21):
        ing = meal.get(f"strIngredient{i}", "").strip()
        measure = meal.get(f"strMeasure{i}", "").strip()
        if ing:
            ingredients.append(f"{measure} {ing}".strip())

    lines = [f"🍳 **{name}**"]
    if category or area:
        lines.append(f"  {category}" + (f" ({area})" if area else ""))
    if ingredients:
        lines.append("\n**Ingredients:**")
        for ing in ingredients:
            lines.append(f"  • {ing}")
    if instructions:
        # First 500 chars of instructions
        short = instructions[:500].strip()
        if len(instructions) > 500:
            short += "..."
        lines.append(f"\n**Instructions:**\n{short}")
    return "\n".join(lines)


async def search_by_name(query: str) -> list[dict]:
    data = await _fetch_json(f"{_BASE_URL}/search.php?s={query.replace(' ', '%20')}")
    return data.get("meals") or [] if data else []


async def search_by_ingredient(ingredient: str) -> list[dict]:
    data = await _fetch_json(f"{_BASE_URL}/filter.php?i={ingredient.replace(' ', '%20')}")
    return data.get("meals") or [] if data else []


async def get_random() -> dict | None:
    data = await _fetch_json(f"{_BASE_URL}/random.php")
    meals = data.get("meals") or [] if data else []
    return meals[0] if meals else None


async def get_meal_detail(meal_id: str) -> dict | None:
    data = await _fetch_json(f"{_BASE_URL}/lookup.php?i={meal_id}")
    meals = data.get("meals") or [] if data else []
    return meals[0] if meals else None


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    query = intent.get("query", "") or intent.get("user_query", "")

    if action == "recipe_search":
        text = re.sub(r"\b(?:find|search|get|show)\s+(?:a\s+)?recipe\s*(?:for|of)?\s*", "", query, flags=re.IGNORECASE)
        text = text.strip()
        if not text:
            await ctx.reply("What recipe are you looking for?")
            return True
        meals = await search_by_name(text)
        if not meals:
            await ctx.reply(f"No recipes found for \"{text}\". Try a different search term.")
            return True
        if len(meals) == 1:
            await ctx.reply(_format_meal(meals[0]))
        else:
            lines = [f"🍳 **{len(meals)} recipes found for \"{text}\":**\n"]
            for m in meals[:8]:
                lines.append(f"  • **{m['strMeal']}** ({m.get('strCategory', '')})")
            await ctx.reply("\n".join(lines))
        return True

    elif action == "recipe_by_ingredient":
        text = re.sub(r"\b(?:what\s+can\s+I\s+(?:make|cook)\s+with|recipes?\s+(?:with|using))\b", "", query, flags=re.IGNORECASE)
        text = text.strip().strip("? ")
        if not text:
            await ctx.reply("What ingredients do you have?")
            return True
        # Use first ingredient for API (it only supports one)
        ingredients = [i.strip() for i in re.split(r"[,&]|\band\b", text) if i.strip()]
        primary = ingredients[0] if ingredients else text
        meals = await search_by_ingredient(primary)
        if not meals:
            await ctx.reply(f"No recipes found with \"{primary}\".")
            return True
        lines = [f"🍳 **Recipes with {primary}** ({len(meals)} found):\n"]
        for m in meals[:10]:
            lines.append(f"  • **{m['strMeal']}**")
        await ctx.reply("\n".join(lines))
        return True

    elif action == "recipe_random":
        meal = await get_random()
        if not meal:
            await ctx.reply("Couldn't fetch a recipe. Try again.")
            return True
        await ctx.reply(_format_meal(meal))
        return True

    return False
