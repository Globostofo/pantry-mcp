"""Remote MCP server exposing Romain's Grocy (food stock, recipes, meal plan) to claude.ai."""

from __future__ import annotations

import os
import sys
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from pantry_mcp.grocy import Grocy, GrocyError
from pantry_mcp.pantry import Pantry

INSTRUCTIONS = """\
This server gives access to Romain's Grocy instance: his food stock, products, recipes,
meal plan and shopping list. It is currently read-only.

- Product, recipe and location names are in French; answer Romain in French.
- Amounts are shown in the unit Grocy uses for them (stock unit, or the unit chosen in the
  recipe / shopping list / meal plan).
- "Overdue" means the best before date has passed (usually still edible); "expired" means
  the expiration date has passed (should be thrown away).
- Prefer the dedicated tools; use `query_objects` only for data they do not cover.
"""

UPSTREAM_SCOPES = "openid profile email offline_access"
READ_ONLY = {"readOnlyHint": True}


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        sys.exit(f"Missing environment variable {name}")
    return value


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
        # claude.ai requests no scope; without offline_access the SSO issues no refresh
        # token and the connector is logged out when the access token expires
        extra_authorize_params={"scope": UPSTREAM_SCOPES},
        allowed_client_redirect_uris=[
            "https://claude.ai/api/mcp/auth_callback",
            "https://claude.com/api/mcp/auth_callback",
        ],
    )


def token_username() -> str | None:
    token = get_access_token()
    return (token.claims or {}).get("preferred_username") if token else None


def build_server(
    pantry: Pantry, auth=None, allowed_user: str | None = None, current_user=token_username
) -> FastMCP:
    """`allowed_user` is checked on every call, on top of the SSO policy binding,
    so that a missing binding cannot open Grocy to other SSO accounts."""
    mcp = FastMCP(name="pantry", instructions=INSTRUCTIONS, auth=auth)

    async def call(fn, *args):
        if allowed_user is not None and current_user() != allowed_user:
            raise ToolError("Access denied: this account is not allowed to use the pantry")
        try:
            return await fn(*args)
        except GrocyError as e:
            raise ToolError(str(e)) from e

    @mcp.tool(annotations=READ_ONLY)
    async def stock() -> str:
        """Current stock: every product in stock with amount, opened amount, next due date
        and location."""
        return await call(pantry.stock)

    @mcp.tool(annotations=READ_ONLY)
    async def stock_alerts(due_soon_days: int = 5) -> str:
        """Products that are expired, overdue, due within `due_soon_days` days, or below
        their minimum stock amount."""
        return await call(pantry.stock_alerts, due_soon_days)

    @mcp.tool(annotations=READ_ONLY)
    async def product(name_or_id: str) -> str:
        """Details of one product, by id or name (exact or partial): stock per location,
        due date, minimum stock, last purchase and prices."""
        return await call(pantry.product, name_or_id)

    @mcp.tool(annotations=READ_ONLY)
    async def recipes() -> str:
        """All recipes, each with whether it can be made with the current stock."""
        return await call(pantry.recipes)

    @mcp.tool(annotations=READ_ONLY)
    async def recipe(name_or_id: str) -> str:
        """One recipe, by id or name: ingredients with stock status, and instructions."""
        return await call(pantry.recipe, name_or_id)

    @mcp.tool(annotations=READ_ONLY)
    async def meal_plan(start: str = "", end: str = "") -> str:
        """Meal plan between two dates (YYYY-MM-DD, inclusive). Defaults: from today,
        for 7 days."""
        return await call(pantry.meal_plan, start, end)

    @mcp.tool(annotations=READ_ONLY)
    async def shopping_list() -> str:
        """Items on the shopping list(s)."""
        return await call(pantry.shopping_list)

    @mcp.tool(annotations=READ_ONLY)
    async def query_objects(
        entity: str, filters: list[str] | None = None, order: str = "", limit: int = 50
    ) -> str:
        """Raw rows of any Grocy entity (e.g. "products", "locations", "stock_log",
        "product_groups", "chores", "tasks"), as JSON. `filters` are Grocy conditions
        `<field><op><value>` with op one of = != ~ (contains) !~ < > <= >= § (regex),
        e.g. ["name~pâtes"]. `order` is "<field>" or "<field>:desc". At most 200 rows."""
        return await call(pantry.query_objects, entity, filters, order, limit)

    @mcp.tool(annotations=READ_ONLY)
    async def system_info() -> str:
        """Grocy version and server information."""
        return await call(pantry.system_info)

    return mcp


def main() -> None:
    base_url = env("BASE_URL")
    grocy = Grocy(env("GROCY_URL"), env("GROCY_API_KEY"))
    auth = build_auth(base_url)
    allowed_user = env("ALLOWED_USERNAME") if auth else None
    mcp = build_server(Pantry(grocy), auth, allowed_user)
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(env("PORT", "8000")),
        path="/mcp",
        allowed_hosts=[urlparse(base_url).netloc],
    )


if __name__ == "__main__":
    main()
