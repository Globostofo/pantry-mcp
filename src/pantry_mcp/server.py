"""Remote MCP server exposing a Grocy instance (food stock, recipes, meal plan) to AI agents."""

from __future__ import annotations

import inspect
import os
import sys
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from pantry_mcp.actions import Actions, Ingredient
from pantry_mcp.grocy import Grocy, GrocyError
from pantry_mcp.pantry import Pantry

INSTRUCTIONS = """\
This server gives access to a Grocy instance: food stock, products, recipes, meal plan and
shopping list, to read and to write. You maintain the data on behalf of the user.

- Names are in the language used in this Grocy; answer in the user's language.
- Amounts are shown in the unit Grocy uses for them (stock unit, or the unit chosen in the
  recipe / shopping list / meal plan).
- "Overdue" means the best before date has passed (usually still edible); "expired" means
  the expiration date has passed (should be thrown away).
- Prefer the dedicated tools; use `query_objects` only for data they do not cover.

Writing:
- Writes take exact names (or ids), never partial matches: look objects up first, and
  reuse existing products, units and locations instead of creating near-duplicates.
- A product's stock unit can never change once it has been in stock: pick the unit it is
  counted in (piece for eggs, g for pasta, mL for milk). Other units (can, pack…) work
  through quantity unit conversions (`purchase_unit` + `purchase_factor`, or a
  `quantity_unit_conversions` object).
- Amounts given to the dedicated tools are converted from `unit` to the stock unit. The
  generic `create_object` / `update_object` take raw Grocy fields: amounts there are in
  the stock unit.
- Stock changes return a transaction id: mention it, `undo` reverts the change.
"""

UPSTREAM_SCOPES = "openid profile email offline_access"
# Some MCP clients do not show the server instructions to the model, only tool
# descriptions: every rule a tool depends on is repeated in its description.
READS = "Names are in the language used in this Grocy: answer in the user's language."
EXACT_NAMES = (
    "Products, units, locations, recipes, lists and sections are matched by exact name "
    "(ignoring case) or id, never partially: look them up first, and reuse existing ones "
    "instead of creating near-duplicates."
)
AMOUNTS = (
    "`amount` is expressed in `unit` (default: the product's stock unit) and converted to "
    "the stock unit, the unit Grocy stores amounts in."
)
UNDO = "Returns a transaction id: mention it to the user, `undo` reverts the change."
RAW = (
    "Raw Grocy fields, as returned by `query_objects`; amounts are in the product's stock "
    "unit. Prefer the dedicated tools when one fits."
)
READ_ONLY = {"readOnlyHint": True}
ADDITIVE = {"readOnlyHint": False, "destructiveHint": False}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True}


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        sys.exit(f"Missing environment variable {name}")
    return value


def env_list(name: str) -> list[str] | None:
    value = os.environ.get(name, "")
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or None


def build_auth(base_url: str):
    if os.environ.get("PANTRY_NO_AUTH") == "1":
        if urlparse(base_url).scheme == "https":
            sys.exit("PANTRY_NO_AUTH is for local development only")
        return None

    from fastmcp.server.auth.oidc_proxy import OIDCProxy

    return OIDCProxy(
        config_url=env("OIDC_CONFIG_URL"),
        client_id=env("OIDC_CLIENT_ID"),
        client_secret=env("OIDC_CLIENT_SECRET"),
        base_url=base_url,
        jwt_signing_key=env("JWT_SIGNING_KEY"),
        # Some MCP clients request no scope; without offline_access the identity provider
        # issues no refresh token and the client is logged out when the access token expires
        extra_authorize_params={"scope": UPSTREAM_SCOPES},
        # None: any MCP client may register (access is still limited to ALLOWED_USERNAME)
        allowed_client_redirect_uris=env_list("ALLOWED_REDIRECT_URIS"),
    )


def token_username() -> str | None:
    token = get_access_token()
    return (token.claims or {}).get("preferred_username") if token else None


def build_server(
    grocy: Grocy,
    auth=None,
    allowed_user: str | None = None,
    current_user=token_username,
    extra_instructions: str | None = None,
) -> FastMCP:
    """`allowed_user` is checked on every call, on top of the identity provider's own
    access rules, so that a misconfigured provider cannot open Grocy to other accounts.
    `extra_instructions` (the deployment's own conventions) are appended to the server
    instructions and to every tool description."""
    extra = extra_instructions.strip() if extra_instructions else ""
    instructions = f"{INSTRUCTIONS}\n{extra}\n" if extra else INSTRUCTIONS
    mcp = FastMCP(name="pantry", instructions=instructions, auth=auth)
    pantry = Pantry(grocy)
    actions = Actions(grocy)

    def tool(*notes: str, annotations: dict):
        """Register a tool whose description is its docstring, then `notes`, then `extra`."""

        def register(fn):
            parts = [inspect.getdoc(fn), *notes, extra]
            description = "\n\n".join(part for part in parts if part)
            return mcp.tool(annotations=annotations, description=description)(fn)

        return register

    async def call(fn, *args):
        if allowed_user is not None and current_user() != allowed_user:
            raise ToolError("Access denied: this account is not allowed to use the pantry")
        try:
            return await fn(*args)
        except GrocyError as e:
            raise ToolError(str(e)) from e

    @tool(READS, annotations=READ_ONLY)
    async def stock() -> str:
        """Current stock: every product in stock with amount, opened amount, next due date
        and location."""
        return await call(pantry.stock)

    @tool(READS, annotations=READ_ONLY)
    async def stock_alerts(due_soon_days: int = 5) -> str:
        """Products that are expired, overdue, due within `due_soon_days` days, or below
        their minimum stock amount. Overdue: best before date passed, usually still edible.
        Expired: expiration date passed, should be thrown away."""
        return await call(pantry.stock_alerts, due_soon_days)

    @tool(READS, annotations=READ_ONLY)
    async def product(name_or_id: str) -> str:
        """Details of one product, by id or name (exact or partial): stock per location,
        due date, minimum stock, last purchase and prices."""
        return await call(pantry.product, name_or_id)

    @tool(READS, annotations=READ_ONLY)
    async def recipes() -> str:
        """All recipes, each with whether it can be made with the current stock."""
        return await call(pantry.recipes)

    @tool(READS, annotations=READ_ONLY)
    async def recipe(name_or_id: str) -> str:
        """One recipe, by id or name: ingredients with stock status, and instructions."""
        return await call(pantry.recipe, name_or_id)

    @tool(READS, annotations=READ_ONLY)
    async def meal_plan(start: str = "", end: str = "") -> str:
        """Meal plan between two dates (YYYY-MM-DD, inclusive). Defaults: from today,
        for 7 days."""
        return await call(pantry.meal_plan, start, end)

    @tool(READS, annotations=READ_ONLY)
    async def shopping_list() -> str:
        """Items on the shopping list(s)."""
        return await call(pantry.shopping_list)

    @tool(READS, annotations=READ_ONLY)
    async def query_objects(
        entity: str, filters: list[str] | None = None, order: str = "", limit: int = 50
    ) -> str:
        """Raw rows of any Grocy entity (e.g. "products", "locations", "stock_log",
        "product_groups", "chores", "tasks"), as JSON. `filters` are Grocy conditions
        `<field><op><value>` with op one of = != ~ (contains) !~ < > <= >= § (regex),
        e.g. ["name~pasta"]. `order` is "<field>" or "<field>:desc". At most 200 rows."""
        return await call(pantry.query_objects, entity, filters, order, limit)

    @tool(READS, annotations=READ_ONLY)
    async def system_info() -> str:
        """Grocy version and server information."""
        return await call(pantry.system_info)

    # Master data

    @tool(EXACT_NAMES, annotations=ADDITIVE)
    async def create_product(
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
        """Create a product. `stock_unit` is the unit it is counted in and can never change
        once the product has been in stock. `default_best_before_days`: shelf life after
        purchase (-1 = never expires). `purchase_unit` + `purchase_factor` (how many stock
        units in one purchase unit, e.g. can = 400 g) allow buying in another unit.
        `min_stock_amount` (stock unit) puts it on the shopping list when below."""
        return await call(
            actions.create_product, name, stock_unit, location, default_best_before_days,
            purchase_unit, purchase_factor, min_stock_amount,
            default_best_before_days_after_open, product_group, description,
        )

    @tool(EXACT_NAMES, AMOUNTS, annotations=ADDITIVE)
    async def create_recipe(
        name: str, servings: int, ingredients: list[Ingredient], instructions: str = ""
    ) -> str:
        """Create a recipe with its ingredients (existing products, exact names) and its
        instructions (plain text, one step per line). Amounts are for `servings`."""
        return await call(actions.create_recipe, name, servings, ingredients, instructions)

    # Stock

    @tool(EXACT_NAMES, AMOUNTS, UNDO, annotations=ADDITIVE)
    async def add_to_stock(
        product: str,
        amount: float,
        unit: str | None = None,
        best_before_date: str = "",
        location: str = "",
        total_price: float | None = None,
    ) -> str:
        """Add a purchase to stock. `best_before_date` (YYYY-MM-DD) defaults to the
        product's shelf life; `location` to its default location; `total_price` is what
        was paid for this amount."""
        return await call(
            actions.add_to_stock, product, amount, unit, best_before_date, location, total_price
        )

    @tool(EXACT_NAMES, AMOUNTS, UNDO, annotations=DESTRUCTIVE)
    async def consume(
        product: str, amount: float, unit: str | None = None, spoiled: bool = False,
        location: str = "",
    ) -> str:
        """Remove an amount of a product from stock (eaten, used), or thrown away when
        `spoiled`. Opened and soonest-due units are used first."""
        return await call(actions.consume, product, amount, unit, spoiled, location)

    @tool(EXACT_NAMES, AMOUNTS, UNDO, annotations=ADDITIVE)
    async def open_product(product: str, amount: float = 1, unit: str | None = None) -> str:
        """Mark an amount of a product as opened (its due date then follows the product's
        shelf life after opening)."""
        return await call(actions.open_product, product, amount, unit)

    @tool(EXACT_NAMES, AMOUNTS, UNDO, annotations=ADDITIVE)
    async def transfer(
        product: str, amount: float, from_location: str, to_location: str,
        unit: str | None = None,
    ) -> str:
        """Move an amount of a product from one location to another."""
        return await call(actions.transfer, product, amount, from_location, to_location, unit)

    @tool(EXACT_NAMES, AMOUNTS, UNDO, annotations=DESTRUCTIVE)
    async def inventory(
        product: str, new_amount: float, unit: str | None = None, best_before_date: str = "",
        location: str = "",
    ) -> str:
        """Set the real amount of a product in stock; Grocy adds or removes the difference.
        `best_before_date` and `location` apply to an added amount."""
        return await call(actions.inventory, product, new_amount, unit, best_before_date, location)

    @tool(annotations=DESTRUCTIVE)
    async def undo(transaction_id: str) -> str:
        """Revert a stock change by the transaction id it returned."""
        return await call(actions.undo, transaction_id)

    # Recipes, shopping list, meal plan

    @tool(EXACT_NAMES, annotations=DESTRUCTIVE)
    async def cook_recipe(recipe: str, servings: int | None = None) -> str:
        """Consume the in-stock ingredients of a recipe (for `servings`, which also becomes
        the recipe's desired servings). Cannot be undone as a whole."""
        return await call(actions.cook_recipe, recipe, servings)

    @tool(EXACT_NAMES, annotations=ADDITIVE)
    async def add_recipe_missing_to_shopping_list(recipe: str) -> str:
        """Put the ingredients missing for a recipe on the shopping list."""
        return await call(actions.add_recipe_missing_to_shopping_list, recipe)

    @tool(EXACT_NAMES, AMOUNTS, annotations=ADDITIVE)
    async def shopping_list_add(
        product: str = "", amount: float = 1, unit: str | None = None, note: str = "",
        shopping_list: str = "",
    ) -> str:
        """Add a product to the shopping list, or a free-text item (`note` only).
        `shopping_list` defaults to the main list."""
        return await call(actions.shopping_list_add, product, amount, unit, note, shopping_list)

    @tool(EXACT_NAMES, AMOUNTS, annotations=DESTRUCTIVE)
    async def shopping_list_remove(
        product: str, amount: float, unit: str | None = None, shopping_list: str = "",
    ) -> str:
        """Remove an amount of a product from the shopping list."""
        return await call(actions.shopping_list_remove, product, amount, unit, shopping_list)

    @tool(EXACT_NAMES, annotations=ADDITIVE)
    async def shopping_list_add_missing(shopping_list: str = "") -> str:
        """Put every product below its minimum stock amount on the shopping list."""
        return await call(actions.shopping_list_add_missing, shopping_list)

    @tool(EXACT_NAMES, annotations=DESTRUCTIVE)
    async def shopping_list_clear(done_only: bool = True, shopping_list: str = "") -> str:
        """Remove the done items from the shopping list, or every item if `done_only` is
        false."""
        return await call(actions.shopping_list_clear, done_only, shopping_list)

    @tool(EXACT_NAMES, AMOUNTS, annotations=ADDITIVE)
    async def plan_meal(
        day: str, recipe: str = "", product: str = "", amount: float = 1,
        unit: str | None = None, note: str = "", servings: int = 1, section: str = "",
    ) -> str:
        """Add an entry to the meal plan on `day` (YYYY-MM-DD): a recipe (with `servings`),
        a product (with `amount`/`unit`) or a free-text `note`. `section` is a meal plan
        section name (e.g. a meal). Remove an entry with `delete_object("meal_plan", id)`."""
        return await call(
            actions.plan_meal, day, recipe, product, amount, unit, note, servings, section
        )

    # Generic: any other write of the Grocy API

    @tool(RAW, annotations=ADDITIVE)
    async def create_object(entity: str, data: dict) -> str:
        """Create a raw Grocy object (e.g. locations, quantity_units,
        quantity_unit_conversions, product_groups, shopping_lists, meal_plan_sections,
        chores, tasks)."""
        return await call(actions.create_object, entity, data)

    @tool(RAW, annotations=DESTRUCTIVE)
    async def update_object(entity: str, object_id: int, data: dict) -> str:
        """Change fields of a raw Grocy object (only the given fields are changed)."""
        return await call(actions.update_object, entity, object_id, data)

    @tool(RAW, annotations=DESTRUCTIVE)
    async def delete_object(entity: str, object_id: int) -> str:
        """Delete a raw Grocy object. Deleting a product also deletes its stock history,
        conversions, recipe ingredients, meal plan and shopping list entries."""
        return await call(actions.delete_object, entity, object_id)

    return mcp


def main() -> None:
    base_url = env("BASE_URL")
    grocy = Grocy(env("GROCY_URL"), env("GROCY_API_KEY"))
    auth = build_auth(base_url)
    allowed_user = env("ALLOWED_USERNAME") if auth else None
    mcp = build_server(
        grocy, auth, allowed_user, extra_instructions=os.environ.get("PANTRY_INSTRUCTIONS")
    )
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(env("PORT", "8000")),
        path="/mcp",
        allowed_hosts=[urlparse(base_url).netloc],
    )


if __name__ == "__main__":
    main()
