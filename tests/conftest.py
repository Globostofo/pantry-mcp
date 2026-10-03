"""A fake Grocy 4.7 API served through httpx.MockTransport (no network access)."""

from __future__ import annotations

import copy
import json
import operator
import re
from typing import Any

import httpx
import pytest

from pantry_mcp.grocy import Grocy
from pantry_mcp.pantry import NEVER_DUE

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
    "quantity_unit_conversions": CONVERSIONS,
    "recipes_pos": [],
    "product_groups": [],
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


class FakeGrocy:
    """In-memory Grocy: each test gets its own copy of the tables and stock."""

    def __init__(self) -> None:
        self.tables = copy.deepcopy(OBJECTS)
        self.stock = {row["product_id"]: copy.deepcopy(row) for row in STOCK}
        self.transactions: dict[str, tuple[int, float]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        # POST paths that fail, to exercise rollbacks
        self.failing: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("GROCY-API-KEY") != API_KEY:
            return error(401, "Unauthorized")
        path = request.url.path.removeprefix("/api")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if request.method == "POST" and path in self.failing:
            return error(400, f"Simulated failure on {path}")
        if path.startswith("/objects/"):
            return self.objects(request, path.removeprefix("/objects/"), body)
        if request.method == "GET":
            return self.get(request, path)
        return self.post(path, body)

    def objects(self, request: httpx.Request, path: str, body: Any) -> httpx.Response:
        entity, _, object_id = path.partition("/")
        rows = self.tables.get(entity)
        if rows is None:
            return error(400, "Entity does not exist or is not exposed")
        if request.method == "GET":
            for condition in request.url.params.get_list("query[]"):
                rows = [r for r in rows if matches(r, condition)]
            if "limit" in request.url.params:
                rows = rows[: int(request.url.params["limit"])]
            return httpx.Response(200, json=rows)
        if request.method == "POST":
            if entity == "products" and any(r["name"] == body["name"] for r in rows):
                return error(400, "UNIQUE constraint failed: products.name")
            new_id = max((int(r["id"]) for r in rows), default=0) + 1
            rows.append({"id": new_id, **body})
            return httpx.Response(200, json={"created_object_id": new_id})
        row = next((r for r in rows if r["id"] == int(object_id)), None)
        if row is None:
            return error(400, "Object not found")
        if request.method == "PUT":
            row.update(body)
        else:
            rows.remove(row)
        return httpx.Response(204)

    def product_stock(self, product_id: int) -> dict:
        product = next(p for p in self.tables["products"] if p["id"] == product_id)
        return self.stock.setdefault(product_id, {
            "product_id": product_id, "amount": 0, "amount_opened": 0,
            "best_before_date": NEVER_DUE, "product": product,
        })

    def get(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == "/stock":
            return httpx.Response(200, json=[r for r in self.stock.values() if r["amount"] > 0])
        if path == "/stock/volatile":
            assert request.url.params["due_soon_days"] == "5"
            return httpx.Response(200, json={
                "due_products": [self.stock[2]],
                "overdue_products": [],
                "expired_products": [],
                "missing_products": [
                    {"id": 1, "name": "Pâtes", "amount_missing": 200, "is_partly_in_stock": 1}
                ],
            })
        if match := re.fullmatch(r"/stock/products/(\d+)", path):
            row = self.product_stock(int(match[1]))
            return httpx.Response(200, json={
                "product": row["product"], "stock_amount": row["amount"],
                "stock_amount_opened": row["amount_opened"],
                "next_due_date": row["best_before_date"], "location": LOCATIONS[0],
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

    def post(self, path: str, body: Any) -> httpx.Response:
        if match := re.fullmatch(r"/stock/products/(\d+)/(add|consume)", path):
            row = self.product_stock(int(match[1]))
            delta = body["amount"] if match[2] == "add" else -body["amount"]
            if row["amount"] + delta < 0:
                return error(400, "Amount to be consumed cannot be > current stock amount")
            row["amount"] += delta
            transaction_id = f"tx{len(self.transactions) + 1}"
            self.transactions[transaction_id] = (row["product_id"], delta)
            return httpx.Response(200, json=[{"id": 1, "transaction_id": transaction_id}])
        if match := re.fullmatch(r"/stock/transactions/([^/]+)/undo", path):
            if match[1] not in self.transactions:
                return error(400, "This transaction was not found or already undone")
            product_id, delta = self.transactions.pop(match[1])
            self.stock[product_id]["amount"] -= delta
            return httpx.Response(204)
        if path == "/stock/shoppinglist/add-product":
            self.tables["shopping_list"].append({
                "id": 99, "shopping_list_id": body["list_id"], "product_id": body["product_id"],
                "amount": body["product_amount"], "qu_id": body["qu_id"],
                "note": body.get("note"), "done": 0,
            })
            return httpx.Response(204)
        if re.fullmatch(r"/recipes/\d+/consume", path):
            return httpx.Response(204)
        return error(404, f"No route for {path}")


@pytest.fixture
def fake() -> FakeGrocy:
    return FakeGrocy()


@pytest.fixture
def grocy(fake: FakeGrocy) -> Grocy:
    return Grocy("http://grocy.test", API_KEY, transport=httpx.MockTransport(fake.handler))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
