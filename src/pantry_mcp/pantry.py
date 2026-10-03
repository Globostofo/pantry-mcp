"""Read operations on Grocy, rendered as compact text for the MCP client.

Grocy stores every amount (stock, recipe ingredients, shopping list, meal plan) in the
product's stock quantity unit; a `qu_id` next to an amount is only the unit to display it
in, converted with `quantity_unit_conversions_resolved`, as Grocy's own UI does.
"""

from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from pantry_mcp.grocy import Grocy, GrocyError

NEVER_DUE = "2999-12-31"
QUERY_LIMIT_MAX = 200
# Every entity Grocy exposes for listing, minus credentials and sessions
READABLE_ENTITIES = frozenset({
    "products", "chores", "product_barcodes", "batteries", "locations", "quantity_units",
    "quantity_unit_conversions", "shopping_list", "shopping_lists", "shopping_locations",
    "recipes", "recipes_pos", "recipes_nestings", "tasks", "task_categories", "product_groups",
    "equipment", "userfields", "userentities", "userobjects", "meal_plan", "stock_log", "stock",
    "stock_current_locations", "chores_log", "meal_plan_sections", "products_last_purchased",
    "products_average_price", "quantity_unit_conversions_resolved", "recipes_pos_resolved",
    "battery_charge_cycles", "product_barcodes_view", "uihelper_shopping_list",
})


def num(value: Any) -> str:
    f = float(value)
    return str(int(f)) if f.is_integer() else f"{f:.3f}".rstrip("0").rstrip(".")


def by_id(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(r["id"]): r for r in rows}


def plain_text(markup: str | None) -> str:
    if not markup:
        return ""
    text = re.sub(r"<br\s*/?>|</p>|</li>", "\n", markup, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise GrocyError(f"Invalid date {value!r}, expected YYYY-MM-DD") from e


@dataclass
class Units:
    """Quantity units and per-product conversions, loaded once per tool call."""

    units: dict[int, dict[str, Any]]
    products: dict[int, dict[str, Any]]
    factors: dict[tuple[int, int, int], float]

    def name(self, qu_id: Any, amount: Any) -> str:
        unit = self.units.get(int(qu_id)) if qu_id else None
        if unit is None:
            return ""
        if float(amount) == 1:
            return unit["name"]
        return unit.get("name_plural") or unit["name"]

    def qty(self, product_id: Any, stock_amount: Any, qu_id: Any = None) -> str:
        """Render an amount stored in the product's stock unit, in `qu_id` when convertible."""
        product = self.products.get(int(product_id)) if product_id else None
        amount = float(stock_amount)
        if product is None:
            return num(amount)
        stock_qu = int(product["qu_id_stock"])
        target = int(qu_id) if qu_id else stock_qu
        factor = self.factors.get((int(product_id), stock_qu, target))
        if target != stock_qu and factor is not None:
            amount, unit = amount * factor, target
        else:
            unit = stock_qu
        return f"{num(amount)} {self.name(unit, amount)}".strip()


class Pantry:
    def __init__(self, grocy: Grocy) -> None:
        self.grocy = grocy

    async def load_units(self) -> Units:
        units = await self.grocy.objects("quantity_units")
        products = await self.grocy.objects("products")
        conversions = await self.grocy.objects("quantity_unit_conversions_resolved")
        factors = {
            (int(c["product_id"]), int(c["from_qu_id"]), int(c["to_qu_id"])): float(c["factor"])
            for c in conversions
            if c.get("product_id")
        }
        return Units(by_id(units), by_id(products), factors)

    async def find(self, entity: str, name_or_id: str, extra: tuple[str, ...] = ()) -> dict:
        """One object by id or by name: exact name first, then substring match."""
        key = name_or_id.strip()
        if key.isdigit():
            rows = await self.grocy.objects(entity, (f"id={key}", *extra))
        else:
            rows = await self.grocy.objects(entity, (f"name~{key}", *extra))
            exact = [r for r in rows if r["name"].casefold() == key.casefold()]
            rows = exact or rows
        label = entity.removesuffix("s")
        if not rows:
            raise GrocyError(f"No {label} matches {name_or_id!r}")
        if len(rows) > 1:
            names = ", ".join(f"{r['name']} (id {r['id']})" for r in rows[:20])
            raise GrocyError(f"Several {label}s match {name_or_id!r}: {names}")
        return rows[0]

    async def stock(self) -> str:
        rows = await self.grocy.get("/stock")
        if not rows:
            return "The stock is empty"
        units = await self.load_units()
        locations = by_id(await self.grocy.objects("locations"))
        lines = []
        for row in sorted(rows, key=lambda r: r["product"]["name"].casefold()):
            product = row["product"]
            line = f"- {product['name']} (id {product['id']}): {units.qty(product['id'], row['amount'])}"
            if float(row.get("amount_opened") or 0) > 0:
                line += f", {units.qty(product['id'], row['amount_opened'])} opened"
            if row.get("best_before_date") and row["best_before_date"] != NEVER_DUE:
                line += f", due {row['best_before_date']}"
            location = locations.get(int(product["location_id"] or 0))
            if location:
                line += f", {location['name']}"
            lines.append(line)
        return "\n".join(lines)

    async def stock_alerts(self, due_soon_days: int = 5) -> str:
        data = await self.grocy.get("/stock/volatile", {"due_soon_days": due_soon_days})
        units = await self.load_units()
        sections = []
        for key, title in (
            ("expired_products", "Expired (expiration date passed, should be thrown away)"),
            ("overdue_products", "Overdue (best before date passed, usually still edible)"),
            ("due_products", f"Due within {due_soon_days} days"),
        ):
            rows = data.get(key) or []
            if rows:
                lines = [
                    f"- {r['product']['name']}: {units.qty(r['product_id'], r['amount'])}, "
                    f"due {r['best_before_date']}"
                    for r in rows
                ]
                sections.append(f"## {title}\n" + "\n".join(lines))
        missing = data.get("missing_products") or []
        if missing:
            lines = [
                f"- {r['name']}: {units.qty(r['id'], r['amount_missing'])} missing"
                + (" (partly in stock)" if int(r.get("is_partly_in_stock") or 0) else "")
                for r in missing
            ]
            sections.append("## Below minimum stock amount\n" + "\n".join(lines))
        return "\n\n".join(sections) or "Nothing expired, due soon or below minimum stock"

    async def product(self, name_or_id: str) -> str:
        product = await self.find("products", name_or_id)
        details = await self.grocy.get(f"/stock/products/{product['id']}")
        per_location = await self.grocy.get(f"/stock/products/{product['id']}/locations")
        units = await self.load_units()
        pid = product["id"]
        lines = [f"# {product['name']} (id {pid})"]
        description = plain_text(product.get("description"))
        if description:
            lines.append(description)
        stock_line = f"In stock: {units.qty(pid, details.get('stock_amount') or 0)}"
        if float(details.get("stock_amount_opened") or 0) > 0:
            stock_line += f" ({units.qty(pid, details['stock_amount_opened'])} opened)"
        lines.append(stock_line)
        for row in per_location or []:
            lines.append(f"- {row['location_name']}: {units.qty(pid, row['amount'])}")
        fields = (
            ("Next due date", details.get("next_due_date")),
            ("Default location", (details.get("location") or {}).get("name")),
            ("Minimum stock amount", units.qty(pid, product.get("min_stock_amount") or 0)),
            ("Default best before days", product.get("default_best_before_days")),
            ("Last purchased", details.get("last_purchased")),
            ("Last used", details.get("last_used")),
            ("Last price (per stock unit)", details.get("last_price")),
            ("Average price (per stock unit)", details.get("avg_price")),
        )
        for label, value in fields:
            if value not in (None, "", NEVER_DUE):
                lines.append(f"{label}: {value}")
        return "\n".join(lines)

    async def recipes(self) -> str:
        rows = await self.grocy.objects("recipes", ("type=normal",), order="name")
        if not rows:
            return "No recipes"
        fulfillment = {
            int(f["recipe_id"]): f for f in await self.grocy.get("/recipes/fulfillment")
        }
        lines = []
        for recipe in rows:
            line = f"- {recipe['name']} (id {recipe['id']}, {recipe['base_servings']} servings): "
            status = fulfillment.get(int(recipe["id"]))
            if status is None:
                line += "stock status unknown"
            elif int(status["need_fulfilled"]):
                line += "can be made with the current stock"
            else:
                line += f"{status['missing_products_count']} ingredient(s) missing"
                if int(status["need_fulfilled_with_shopping_list"]):
                    line += ", all covered by the shopping list"
            lines.append(line)
        return "\n".join(lines)

    async def recipe(self, name_or_id: str) -> str:
        recipe = await self.find("recipes", name_or_id, ("type=normal",))
        positions = await self.grocy.objects("recipes_pos_resolved", (f"recipe_id={recipe['id']}",))
        units = await self.load_units()
        lines = [
            f"# {recipe['name']} (id {recipe['id']})",
            f"Amounts for {recipe['desired_servings']} servings "
            f"(base recipe: {recipe['base_servings']} servings)",
        ]
        groups: dict[str, list[str]] = defaultdict(list)
        for pos in positions:
            if pos.get("recipe_variable_amount"):
                amount = pos["recipe_variable_amount"]
            else:
                amount = units.qty(pos["product_id"], pos["recipe_amount"], pos.get("qu_id"))
            line = f"- {pos['product_name']}: {amount}"
            if pos.get("note"):
                line += f" ({pos['note']})"
            if int(pos["need_fulfilled"]):
                line += " — in stock"
            else:
                line += f" — missing {units.qty(pos['product_id'], pos['missing_amount'])}"
                if float(pos.get("amount_on_shopping_list") or 0) > 0:
                    line += ", on the shopping list"
            groups[pos.get("ingredient_group") or ""].append(line)
        lines.append("\n## Ingredients")
        for group, group_lines in groups.items():
            if group:
                lines.append(f"### {group}")
            lines.extend(group_lines)
        if not positions:
            lines.append("(none)")
        description = plain_text(recipe.get("description"))
        if description:
            lines += ["\n## Instructions", description]
        return "\n".join(lines)

    async def meal_plan(self, start: str = "", end: str = "") -> str:
        first = parse_day(start) if start else date.today()
        last = parse_day(end) if end else first + timedelta(days=6)
        rows = await self.grocy.objects(
            "meal_plan", (f"day>={first.isoformat()}", f"day<={last.isoformat()}"), order="day"
        )
        if not rows:
            return f"Nothing planned from {first} to {last}"
        recipes = by_id(await self.grocy.objects("recipes"))
        sections = by_id(await self.grocy.objects("meal_plan_sections"))
        units = await self.load_units()
        days: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for row in rows:
            if row["type"] == "recipe":
                recipe = recipes.get(int(row["recipe_id"] or 0), {})
                text = f"{recipe.get('name', '?')} (recipe, {row['recipe_servings']} servings)"
            elif row["type"] == "product":
                product = units.products.get(int(row["product_id"] or 0), {})
                amount = units.qty(row["product_id"], row["product_amount"], row.get("product_qu_id"))
                text = f"{product.get('name', '?')}: {amount}"
            else:
                text = f"Note: {row.get('note') or ''}"
            section = sections.get(int(row.get("section_id") or 0))
            if section and section.get("name"):
                text = f"[{section['name']}] {text}"
            if int(row.get("done") or 0):
                text += " (done)"
            order = int((section or {}).get("sort_number") or 0)
            days[row["day"]].append((order, text))
        lines = []
        for day in sorted(days):
            weekday = date.fromisoformat(day).strftime("%A")
            lines.append(f"## {day} ({weekday})")
            lines += [f"- {text}" for _, text in sorted(days[day], key=lambda e: e[0])]
        return "\n".join(lines)

    async def shopping_list(self) -> str:
        items = await self.grocy.objects("shopping_list")
        if not items:
            return "The shopping list is empty"
        lists = by_id(await self.grocy.objects("shopping_lists"))
        units = await self.load_units()
        grouped: dict[int, list[str]] = defaultdict(list)
        for item in items:
            product = units.products.get(int(item.get("product_id") or 0))
            if product:
                line = f"- {product['name']}: {units.qty(product['id'], item['amount'], item.get('qu_id'))}"
                if item.get("note"):
                    line += f" ({item['note']})"
            else:
                line = f"- {item.get('note') or '?'}"
            if int(item.get("done") or 0):
                line += " (done)"
            grouped[int(item.get("shopping_list_id") or 1)].append(line)
        lines = []
        for list_id, list_lines in sorted(grouped.items()):
            if len(lists) > 1 or len(grouped) > 1:
                lines.append(f"## {lists.get(list_id, {}).get('name', f'List {list_id}')}")
            lines += list_lines
        return "\n".join(lines)

    async def query_objects(
        self, entity: str, filters: list[str] | None = None, order: str = "", limit: int = 50
    ) -> str:
        if entity not in READABLE_ENTITIES:
            raise GrocyError(
                f"Unknown or forbidden entity {entity!r}. Readable entities: "
                + ", ".join(sorted(READABLE_ENTITIES))
            )
        limit = max(1, min(limit, QUERY_LIMIT_MAX))
        rows = await self.grocy.objects(entity, tuple(filters or ()), order or None, limit)
        return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))

    async def system_info(self) -> str:
        return json.dumps(await self.grocy.get("/system/info"), ensure_ascii=False)
