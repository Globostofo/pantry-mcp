"""Write operations on Grocy.

Names are resolved with `exact=True`: a write never targets an object through a partial
name match. Amounts are converted to the product's stock unit before being sent, since
that is the unit Grocy stores every amount in.
"""

from __future__ import annotations

import html
import re
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from pantry_mcp.grocy import Grocy, GrocyError
from pantry_mcp.lookup import Units, find, load_units, num

NEVER_DUE = "2999-12-31"
# Entities Grocy lets the API create, edit and delete, minus API keys
WRITABLE_ENTITIES = frozenset({
    "products", "chores", "product_barcodes", "batteries", "locations", "quantity_units",
    "quantity_unit_conversions", "shopping_list", "shopping_lists", "shopping_locations",
    "recipes", "recipes_pos", "recipes_nestings", "tasks", "task_categories", "product_groups",
    "equipment", "userfields", "userentities", "userobjects", "meal_plan", "meal_plan_sections",
})
TRANSACTION_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


class Ingredient(BaseModel):
    product: str = Field(description="Exact product name or id (create missing products first)")
    amount: float = Field(0, description="Amount for the recipe's servings, in `unit`")
    unit: str | None = Field(None, description="Quantity unit name; default: the product's stock unit")
    note: str | None = Field(None, description='E.g. "finely chopped"')
    group: str | None = Field(None, description='Ingredient group heading, e.g. "Sauce"')
    variable_amount: str | None = Field(
        None, description='Free-text amount shown instead of `amount`, e.g. "to taste"'
    )


def check_day(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as e:
        raise GrocyError(f"Invalid date {value!r}, expected YYYY-MM-DD") from e


def check_entity(entity: str) -> None:
    if entity not in WRITABLE_ENTITIES:
        raise GrocyError(
            f"Entity {entity!r} cannot be written (read-only view, or forbidden). Writable "
            "entities: " + ", ".join(sorted(WRITABLE_ENTITIES))
        )


def to_html(text: str) -> str:
    """Recipe and product descriptions are HTML in Grocy (rich text editor)."""
    paragraphs = [p.strip() for p in text.strip().split("\n") if p.strip()]
    return "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)


def transaction(log: Any) -> str:
    entries = log or []
    ids = list(dict.fromkeys(e.get("transaction_id") for e in entries if e.get("transaction_id")))
    return f" (transaction {ids[0]}, undo with `undo`)" if ids else ""


class Actions:
    def __init__(self, grocy: Grocy) -> None:
        self.grocy = grocy

    async def product(self, name_or_id: str) -> dict[str, Any]:
        return await find(self.grocy, "products", name_or_id, exact=True)

    async def location_id(self, name_or_id: str) -> int:
        return int((await find(self.grocy, "locations", name_or_id, exact=True))["id"])

    async def list_id(self, name_or_id: str) -> int:
        if not name_or_id:
            return 1
        return int((await find(self.grocy, "shopping_lists", name_or_id, exact=True))["id"])

    async def in_stock(self, product: dict[str, Any], units: Units) -> str:
        details = await self.grocy.get(f"/stock/products/{product['id']}")
        return f"{product['name']} now in stock: {units.qty(product['id'], details.get('stock_amount') or 0)}"

    async def stock_amount(self, product: dict[str, Any], amount: float, unit: str | None) -> tuple[float, int, Units]:
        units = await load_units(self.grocy)
        stock_amount, qu = units.to_stock(product["id"], amount, unit)
        return stock_amount, qu, units

    # Master data

    async def create_product(
        self,
        name: str,
        stock_unit: str,
        location: str,
        default_best_before_days: int,
        purchase_unit: str | None = None,
        purchase_factor: float | None = None,
        min_stock_amount: float = 0,
        default_best_before_days_after_open: int = 0,
        product_group: str | None = None,
        description: str | None = None,
    ) -> str:
        existing = await self.grocy.objects("products")
        for row in existing:
            if row["name"].strip().casefold() == name.strip().casefold():
                raise GrocyError(f"Product {row['name']!r} already exists (id {row['id']})")
        units = await load_units(self.grocy)
        stock_qu = units.unit_id(stock_unit)
        purchase_qu = units.unit_id(purchase_unit) if purchase_unit else stock_qu
        if purchase_qu != stock_qu and not purchase_factor:
            raise GrocyError(
                f"purchase_factor is required: how many {stock_unit} are in one {purchase_unit}"
            )
        body: dict[str, Any] = {
            "name": name.strip(),
            "location_id": await self.location_id(location),
            "qu_id_stock": stock_qu,
            "qu_id_purchase": purchase_qu,
            "qu_id_consume": stock_qu,
            "qu_id_price": stock_qu,
            "min_stock_amount": min_stock_amount,
            "default_best_before_days": default_best_before_days,
            "default_best_before_days_after_open": default_best_before_days_after_open,
        }
        if product_group:
            group = await find(self.grocy, "product_groups", product_group, exact=True)
            body["product_group_id"] = int(group["id"])
        if description:
            body["description"] = to_html(description)
        product_id = (await self.grocy.post("/objects/products", body))["created_object_id"]
        summary = f"Created product {name.strip()} (id {product_id}), stock unit {stock_unit}"
        if purchase_qu != stock_qu:
            try:
                await self.grocy.post("/objects/quantity_unit_conversions", {
                    "product_id": product_id,
                    "from_qu_id": purchase_qu,
                    "to_qu_id": stock_qu,
                    "factor": purchase_factor,
                })
            except GrocyError:
                await self.grocy.delete(f"/objects/products/{product_id}")
                raise
            summary += f", 1 {purchase_unit} = {num(purchase_factor)} {stock_unit}"
        return summary

    async def create_recipe(
        self, name: str, servings: int, ingredients: list[Ingredient], instructions: str = ""
    ) -> str:
        for row in await self.grocy.objects("recipes", ("type=normal",)):
            if row["name"].strip().casefold() == name.strip().casefold():
                raise GrocyError(f"Recipe {row['name']!r} already exists (id {row['id']})")
        # Resolve everything before writing anything
        units = await load_units(self.grocy)
        positions = []
        for ingredient in ingredients:
            product = await self.product(ingredient.product)
            amount, qu = units.to_stock(product["id"], ingredient.amount, ingredient.unit)
            positions.append({
                "product_id": int(product["id"]),
                "amount": amount,
                "qu_id": qu,
                "note": ingredient.note,
                "ingredient_group": ingredient.group,
                "variable_amount": ingredient.variable_amount,
            })
        recipe_id = (await self.grocy.post("/objects/recipes", {
            "name": name.strip(),
            "base_servings": servings,
            "desired_servings": servings,
            "description": to_html(instructions) if instructions else None,
        }))["created_object_id"]
        created: list[int] = []
        try:
            for position in positions:
                body = {k: v for k, v in position.items() if v is not None}
                created.append(
                    (await self.grocy.post("/objects/recipes_pos", {"recipe_id": recipe_id, **body}))[
                        "created_object_id"
                    ]
                )
        except GrocyError:
            # Grocy does not cascade recipe deletions to recipes_pos
            for position_id in created:
                await self.grocy.delete(f"/objects/recipes_pos/{position_id}")
            await self.grocy.delete(f"/objects/recipes/{recipe_id}")
            raise
        return f"Created recipe {name.strip()} (id {recipe_id}) with {len(positions)} ingredient(s)"

    # Stock

    async def add_to_stock(
        self,
        product: str,
        amount: float,
        unit: str | None = None,
        best_before_date: str = "",
        location: str = "",
        total_price: float | None = None,
    ) -> str:
        p = await self.product(product)
        stock_amount, qu, units = await self.stock_amount(p, amount, unit)
        body: dict[str, Any] = {"amount": stock_amount, "transaction_type": "purchase"}
        if best_before_date:
            body["best_before_date"] = check_day(best_before_date)
        elif int(p.get("default_best_before_days") or 0) == 0:
            # Grocy would use today, making the product due right away
            raise GrocyError(
                f"{p['name']} has no default best before days: pass best_before_date "
                f"(use {NEVER_DUE} for products that never expire)"
            )
        if location:
            body["location_id"] = await self.location_id(location)
        if total_price is not None and stock_amount:
            body["price"] = total_price / stock_amount
        log = await self.grocy.post(f"/stock/products/{p['id']}/add", body)
        added = units.qty(p["id"], stock_amount, qu)
        return f"Added {added} of {p['name']}{transaction(log)}. {await self.in_stock(p, units)}"

    async def consume(
        self, product: str, amount: float, unit: str | None = None, spoiled: bool = False,
        location: str = "",
    ) -> str:
        p = await self.product(product)
        stock_amount, qu, units = await self.stock_amount(p, amount, unit)
        body: dict[str, Any] = {
            "amount": stock_amount, "transaction_type": "consume", "spoiled": spoiled,
        }
        if location:
            body["location_id"] = await self.location_id(location)
        log = await self.grocy.post(f"/stock/products/{p['id']}/consume", body)
        verb = "Threw away" if spoiled else "Consumed"
        return f"{verb} {units.qty(p['id'], stock_amount, qu)} of {p['name']}{transaction(log)}. {await self.in_stock(p, units)}"

    async def open_product(self, product: str, amount: float = 1, unit: str | None = None) -> str:
        p = await self.product(product)
        stock_amount, qu, units = await self.stock_amount(p, amount, unit)
        log = await self.grocy.post(f"/stock/products/{p['id']}/open", {"amount": stock_amount})
        return f"Marked {units.qty(p['id'], stock_amount, qu)} of {p['name']} as opened{transaction(log)}"

    async def transfer(
        self, product: str, amount: float, from_location: str, to_location: str,
        unit: str | None = None,
    ) -> str:
        p = await self.product(product)
        stock_amount, qu, units = await self.stock_amount(p, amount, unit)
        log = await self.grocy.post(f"/stock/products/{p['id']}/transfer", {
            "amount": stock_amount,
            "location_id_from": await self.location_id(from_location),
            "location_id_to": await self.location_id(to_location),
        })
        return (
            f"Moved {units.qty(p['id'], stock_amount, qu)} of {p['name']} from {from_location} "
            f"to {to_location}{transaction(log)}"
        )

    async def inventory(
        self, product: str, new_amount: float, unit: str | None = None,
        best_before_date: str = "", location: str = "",
    ) -> str:
        p = await self.product(product)
        stock_amount, _, units = await self.stock_amount(p, new_amount, unit)
        body: dict[str, Any] = {"new_amount": stock_amount}
        if best_before_date:
            body["best_before_date"] = check_day(best_before_date)
        if location:
            body["location_id"] = await self.location_id(location)
        log = await self.grocy.post(f"/stock/products/{p['id']}/inventory", body)
        return f"Inventory saved{transaction(log)}. {await self.in_stock(p, units)}"

    async def undo(self, transaction_id: str) -> str:
        if not TRANSACTION_ID.match(transaction_id):
            raise GrocyError(f"Invalid transaction id {transaction_id!r}")
        await self.grocy.post(f"/stock/transactions/{transaction_id}/undo")
        return f"Undone transaction {transaction_id}"

    # Recipes, shopping list, meal plan

    async def recipe(self, name_or_id: str) -> dict[str, Any]:
        return await find(self.grocy, "recipes", name_or_id, ("type=normal",), exact=True)

    async def cook_recipe(self, recipe: str, servings: int | None = None) -> str:
        r = await self.recipe(recipe)
        if servings and int(r["desired_servings"]) != servings:
            await self.grocy.put(f"/objects/recipes/{r['id']}", {"desired_servings": servings})
        await self.grocy.post(f"/recipes/{r['id']}/consume")
        return (
            f"Consumed the in-stock ingredients of {r['name']} for "
            f"{servings or r['desired_servings']} servings"
        )

    async def add_recipe_missing_to_shopping_list(self, recipe: str) -> str:
        r = await self.recipe(recipe)
        await self.grocy.post(f"/recipes/{r['id']}/add-not-fulfilled-products-to-shoppinglist")
        return f"Added the missing ingredients of {r['name']} to the shopping list"

    async def shopping_list_add(
        self, product: str = "", amount: float = 1, unit: str | None = None, note: str = "",
        shopping_list: str = "",
    ) -> str:
        list_id = await self.list_id(shopping_list)
        if not product:
            if not note:
                raise GrocyError("Give a product, or a note for a free-text item")
            await self.grocy.post("/objects/shopping_list", {
                "note": note, "amount": amount, "shopping_list_id": list_id,
            })
            return f"Added {note!r} to the shopping list"
        p = await self.product(product)
        stock_amount, qu, units = await self.stock_amount(p, amount, unit)
        body: dict[str, Any] = {
            "product_id": int(p["id"]), "product_amount": stock_amount, "qu_id": qu,
            "list_id": list_id,
        }
        if note:
            body["note"] = note
        await self.grocy.post("/stock/shoppinglist/add-product", body)
        return f"Added {units.qty(p['id'], stock_amount, qu)} of {p['name']} to the shopping list"

    async def shopping_list_remove(
        self, product: str, amount: float, unit: str | None = None, shopping_list: str = "",
    ) -> str:
        p = await self.product(product)
        stock_amount, qu, units = await self.stock_amount(p, amount, unit)
        await self.grocy.post("/stock/shoppinglist/remove-product", {
            "product_id": int(p["id"]), "product_amount": stock_amount,
            "list_id": await self.list_id(shopping_list),
        })
        return f"Removed {units.qty(p['id'], stock_amount, qu)} of {p['name']} from the shopping list"

    async def shopping_list_add_missing(self, shopping_list: str = "") -> str:
        await self.grocy.post("/stock/shoppinglist/add-missing-products", {
            "list_id": await self.list_id(shopping_list),
        })
        return "Added the products below their minimum stock amount to the shopping list"

    async def shopping_list_clear(self, done_only: bool = True, shopping_list: str = "") -> str:
        await self.grocy.post("/stock/shoppinglist/clear", {
            "list_id": await self.list_id(shopping_list), "done_only": done_only,
        })
        return "Removed the done items" if done_only else "Cleared the shopping list"

    async def plan_meal(
        self, day: str, recipe: str = "", product: str = "", amount: float = 1,
        unit: str | None = None, note: str = "", servings: int = 1, section: str = "",
    ) -> str:
        if sum(bool(x) for x in (recipe, product)) > 1 or not (recipe or product or note):
            raise GrocyError("Give exactly one of recipe, product or note")
        body: dict[str, Any] = {"day": check_day(day)}
        if recipe:
            r = await self.recipe(recipe)
            body |= {"type": "recipe", "recipe_id": int(r["id"]), "recipe_servings": servings}
            what = f"{r['name']} ({servings} servings)"
        elif product:
            p = await self.product(product)
            stock_amount, qu, units = await self.stock_amount(p, amount, unit)
            body |= {
                "type": "product", "product_id": int(p["id"]), "product_amount": stock_amount,
                "product_qu_id": qu,
            }
            what = f"{p['name']}: {units.qty(p['id'], stock_amount, qu)}"
        else:
            body |= {"type": "note", "note": note}
            what = f"note {note!r}"
        if section:
            s = await find(self.grocy, "meal_plan_sections", section, exact=True)
            body["section_id"] = int(s["id"])
        entry_id = (await self.grocy.post("/objects/meal_plan", body))["created_object_id"]
        return f"Planned {what} on {body['day']} (meal_plan id {entry_id})"

    # Generic

    async def create_object(self, entity: str, data: dict[str, Any]) -> str:
        check_entity(entity)
        created = await self.grocy.post(f"/objects/{entity}", data)
        return f"Created {entity} id {created['created_object_id']}"

    async def update_object(self, entity: str, object_id: int, data: dict[str, Any]) -> str:
        check_entity(entity)
        await self.grocy.put(f"/objects/{entity}/{int(object_id)}", data)
        return f"Updated {entity} id {object_id}"

    async def delete_object(self, entity: str, object_id: int) -> str:
        check_entity(entity)
        await self.grocy.delete(f"/objects/{entity}/{int(object_id)}")
        return f"Deleted {entity} id {object_id}"
