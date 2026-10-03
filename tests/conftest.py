"""A fake Grocy 4.7 API served through httpx.MockTransport (no network access)."""

from __future__ import annotations

import operator

import httpx
import pytest

from pantry_mcp.grocy import Grocy
from pantry_mcp.pantry import NEVER_DUE, Pantry

API_KEY = "test-key"

UNITS = [
    {"id": 1, "name": "gramme", "name_plural": "grammes"},
    {"id": 2, "name": "pièce", "name_plural": "pièces"},
    {"id": 3, "name": "boîte", "name_plural": "boîtes"},
]
PRODUCTS = [
    {"id": 1, "name": "Pâtes", "qu_id_stock": 1, "location_id": 1, "min_stock_amount": 500,
     "default_best_before_days": 365, "description": "<p>Penne &amp; fusilli</p>"},
    {"id": 2, "name": "Œufs", "qu_id_stock": 2, "location_id": 2, "min_stock_amount": 0,
     "default_best_before_days": 21, "description": None},
    {"id": 3, "name": "Tomates pelées", "qu_id_stock": 1, "location_id": 1, "min_stock_amount": 0,
     "default_best_before_days": 0, "description": None},
]
LOCATIONS = [{"id": 1, "name": "Placard"}, {"id": 2, "name": "Frigo"}]
# One can of peeled tomatoes is 400 g
CONVERSIONS = [
    {"id": 1, "product_id": 3, "from_qu_id": 1, "to_qu_id": 3, "factor": 0.0025},
    {"id": 2, "product_id": 3, "from_qu_id": 3, "to_qu_id": 1, "factor": 400},
]
RECIPES = [
    {"id": 1, "name": "Omelette", "type": "normal", "base_servings": 1, "desired_servings": 2,
     "description": "<p>Battre les œufs.</p><p>Cuire 3 minutes.</p>"},
    {"id": 2, "name": "Sauce tomate", "type": "normal", "base_servings": 4, "desired_servings": 4,
     "description": None},
    {"id": -1, "name": "2026-10-05", "type": "mealplan-day", "base_servings": 1,
     "desired_servings": 1, "description": None},
]
RECIPES_POS_RESOLVED = [
    {"id": 1, "recipe_id": 1, "product_id": 2, "product_name": "Œufs", "recipe_amount": 6,
     "qu_id": 2, "need_fulfilled": 1, "missing_amount": 0, "amount_on_shopping_list": 0,
     "note": None, "ingredient_group": None, "recipe_variable_amount": None},
    {"id": 2, "recipe_id": 2, "product_id": 3, "product_name": "Tomates pelées",
     "recipe_amount": 800, "qu_id": 3, "need_fulfilled": 0, "missing_amount": 400,
     "amount_on_shopping_list": 400, "note": "entières", "ingredient_group": "Sauce",
     "recipe_variable_amount": None},
    {"id": 3, "recipe_id": 2, "product_id": 1, "product_name": "Pâtes", "recipe_amount": 0,
     "qu_id": 1, "need_fulfilled": 1, "missing_amount": 0, "amount_on_shopping_list": 0,
     "note": None, "ingredient_group": "Service", "recipe_variable_amount": "à volonté"},
]
MEAL_PLAN = [
    {"id": 1, "day": "2026-10-05", "type": "recipe", "recipe_id": 1, "recipe_servings": 2,
     "section_id": 2, "done": 0},
    {"id": 2, "day": "2026-10-05", "type": "product", "product_id": 3, "product_amount": 400,
     "product_qu_id": 3, "section_id": 1, "done": 1},
    {"id": 3, "day": "2026-10-06", "type": "note", "note": "Restaurant", "section_id": None,
     "done": 0},
    {"id": 4, "day": "2026-11-01", "type": "note", "note": "Out of range", "section_id": None,
     "done": 0},
]
MEAL_PLAN_SECTIONS = [
    {"id": 1, "name": "Déjeuner", "sort_number": 1},
    {"id": 2, "name": "Dîner", "sort_number": 2},
]
SHOPPING_LIST = [
    {"id": 1, "shopping_list_id": 1, "product_id": 3, "amount": 400, "qu_id": 3, "note": None,
     "done": 0},
    {"id": 2, "shopping_list_id": 1, "product_id": None, "amount": 1, "qu_id": None,
     "note": "Papier cuisson", "done": 1},
]
OBJECTS = {
    "quantity_units": UNITS,
    "products": PRODUCTS,
    "locations": LOCATIONS,
    "quantity_unit_conversions_resolved": CONVERSIONS,
    "recipes": RECIPES,
    "recipes_pos_resolved": RECIPES_POS_RESOLVED,
    "meal_plan": MEAL_PLAN,
    "meal_plan_sections": MEAL_PLAN_SECTIONS,
    "shopping_list": SHOPPING_LIST,
    "shopping_lists": [{"id": 1, "name": "Liste de courses"}],
    "api_keys": [{"id": 1, "api_key": "secret"}],
}
STOCK = [
    {"product_id": 1, "amount": 1000, "amount_opened": 250, "best_before_date": "2026-12-01",
     "product": PRODUCTS[0]},
    {"product_id": 2, "amount": 6, "amount_opened": 0, "best_before_date": "2026-10-08",
     "product": PRODUCTS[1]},
    {"product_id": 3, "amount": 400, "amount_opened": 0, "best_before_date": NEVER_DUE,
     "product": PRODUCTS[2]},
]
OPERATORS = {
    ">=": operator.ge, "<=": operator.le, "!=": operator.ne, "=": operator.eq,
    "~": lambda a, b: str(b).casefold() in str(a).casefold(),
}


def matches(row: dict, condition: str) -> bool:
    for op, fn in OPERATORS.items():
        if op in condition:
            field, value = condition.split(op, 1)
            actual = row.get(field)
            if isinstance(actual, int) and value.lstrip("-").isdigit():
                return fn(actual, int(value))
            return fn(str(actual), value)
    raise AssertionError(f"Unsupported condition {condition}")


def error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error_message": message})


def handler(request: httpx.Request) -> httpx.Response:
    if request.headers.get("GROCY-API-KEY") != API_KEY:
        return error(401, "Unauthorized")
    path = request.url.path.removeprefix("/api")
    if path.startswith("/objects/"):
        rows = OBJECTS.get(path.removeprefix("/objects/"))
        if rows is None:
            return error(400, "Entity does not exist or is not exposed")
        for condition in request.url.params.get_list("query[]"):
            rows = [r for r in rows if matches(r, condition)]
        if "limit" in request.url.params:
            rows = rows[: int(request.url.params["limit"])]
        return httpx.Response(200, json=rows)
    if path == "/stock":
        return httpx.Response(200, json=STOCK)
    if path == "/stock/volatile":
        assert request.url.params["due_soon_days"] == "5"
        return httpx.Response(200, json={
            "due_products": [STOCK[1]],
            "overdue_products": [],
            "expired_products": [],
            "missing_products": [
                {"id": 1, "name": "Pâtes", "amount_missing": 200, "is_partly_in_stock": 1}
            ],
        })
    if path == "/stock/products/1":
        return httpx.Response(200, json={
            "product": PRODUCTS[0], "stock_amount": 1000, "stock_amount_opened": 250,
            "next_due_date": "2026-12-01", "location": LOCATIONS[0],
            "last_purchased": "2026-09-28", "last_used": None, "last_price": 0.0021,
            "avg_price": None,
        })
    if path == "/stock/products/1/locations":
        return httpx.Response(200, json=[
            {"product_id": 1, "location_id": 1, "location_name": "Placard", "amount": 1000}
        ])
    if path == "/recipes/fulfillment":
        return httpx.Response(200, json=[
            {"recipe_id": 1, "need_fulfilled": 1, "need_fulfilled_with_shopping_list": 1,
             "missing_products_count": 0},
            {"recipe_id": 2, "need_fulfilled": 0, "need_fulfilled_with_shopping_list": 1,
             "missing_products_count": 1},
        ])
    if path == "/system/info":
        return httpx.Response(200, json={"grocy_version": {"Version": "4.7.1"}})
    return error(404, f"No route for {path}")


@pytest.fixture
def pantry() -> Pantry:
    return Pantry(Grocy("http://grocy.test", API_KEY, transport=httpx.MockTransport(handler)))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
