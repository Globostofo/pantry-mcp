import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from pantry_mcp.grocy import Grocy
from pantry_mcp.server import build_server

READ_TOOLS = {
    "stock", "stock_alerts", "product", "recipes", "recipe", "meal_plan", "shopping_list",
    "query_objects", "system_info",
}


async def text(client: Client, tool: str, args: dict | None = None) -> str:
    return (await client.call_tool(tool, args or {})).data


@pytest.mark.anyio
async def test_only_read_tools_are_read_only(grocy: Grocy) -> None:
    async with Client(build_server(grocy)) as client:
        tools = await client.list_tools()
        read_only = {t.name for t in tools if t.annotations.read_only_hint}
        assert read_only == READ_TOOLS
        assert len(tools) > len(READ_TOOLS)


@pytest.mark.anyio
async def test_stock(grocy: Grocy) -> None:
    async with Client(build_server(grocy)) as client:
        lines = (await text(client, "stock")).splitlines()
    assert lines == [
        "- Pâtes (id 1): 1000 grammes, 250 grammes opened, due 2026-12-01, Placard",
        "- Tomates pelées (id 3): 400 grammes, Placard",
        "- Œufs (id 2): 6 pièces, due 2026-10-08, Frigo",
    ]


@pytest.mark.anyio
async def test_stock_alerts(grocy: Grocy) -> None:
    async with Client(build_server(grocy)) as client:
        alerts = await text(client, "stock_alerts")
    assert alerts == (
        "## Due within 5 days\n- Œufs: 6 pièces, due 2026-10-08\n\n"
        "## Below minimum stock amount\n- Pâtes: 200 grammes missing (partly in stock)"
    )


@pytest.mark.anyio
async def test_product(grocy: Grocy) -> None:
    async with Client(build_server(grocy)) as client:
        details = await text(client, "product", {"name_or_id": "pâtes"})
        assert details.splitlines()[:4] == [
            "# Pâtes (id 1)",
            "Penne & fusilli",
            "In stock: 1000 grammes (250 grammes opened)",
            "- Placard: 1000 grammes",
        ]
        assert "Minimum stock amount: 500 grammes" in details
        assert "Last used" not in details
        assert (await text(client, "product", {"name_or_id": "1"})) == details

        with pytest.raises(ToolError, match=r"Several products match 'e': .*Pâtes \(id 1\)"):
            await client.call_tool("product", {"name_or_id": "e"})
        with pytest.raises(ToolError, match="No product named 'riz'"):
            await client.call_tool("product", {"name_or_id": "riz"})


@pytest.mark.anyio
async def test_recipes(grocy: Grocy) -> None:
    async with Client(build_server(grocy)) as client:
        listing = await text(client, "recipes")
        assert listing.splitlines() == [
            "- Omelette (id 1, 1 servings): can be made with the current stock",
            "- Sauce tomate (id 2, 4 servings): 1 ingredient(s) missing, "
            "all covered by the shopping list",
        ]

        omelette = await text(client, "recipe", {"name_or_id": "omelette"})
        assert omelette == (
            "# Omelette (id 1)\nAmounts for 2 servings (base recipe: 1 servings)\n"
            "\n## Ingredients\n- Œufs: 6 pièces — in stock\n"
            "\n## Instructions\nBattre les œufs.\nCuire 3 minutes."
        )

        sauce = await text(client, "recipe", {"name_or_id": "2"})
        assert "### Sauce\n- Tomates pelées: 2 boîtes (entières) — missing 400 grammes, " \
            "on the shopping list" in sauce
        assert "### Service\n- Pâtes: à volonté — in stock" in sauce

        # Meal plan shadow recipes are not real recipes
        with pytest.raises(ToolError, match="No recipe named"):
            await client.call_tool("recipe", {"name_or_id": "-1"})


@pytest.mark.anyio
async def test_meal_plan(grocy: Grocy) -> None:
    async with Client(build_server(grocy)) as client:
        plan = await text(client, "meal_plan", {"start": "2026-10-05", "end": "2026-10-11"})
        assert plan == (
            "## 2026-10-05 (Monday)\n"
            "- [Déjeuner] Tomates pelées: 1 boîte (done)\n"
            "- [Dîner] Omelette (recipe, 2 servings)\n"
            "## 2026-10-06 (Tuesday)\n"
            "- Note: Restaurant"
        )
        empty = await text(client, "meal_plan", {"start": "2027-01-01", "end": "2027-01-02"})
        assert empty == "Nothing planned from 2027-01-01 to 2027-01-02"
        with pytest.raises(ToolError, match="Invalid date"):
            await client.call_tool("meal_plan", {"start": "tomorrow"})


@pytest.mark.anyio
async def test_shopping_list(grocy: Grocy) -> None:
    async with Client(build_server(grocy)) as client:
        assert await text(client, "shopping_list") == (
            "- Tomates pelées: 1 boîte\n- Papier cuisson (done)"
        )


@pytest.mark.anyio
async def test_query_objects(grocy: Grocy) -> None:
    async with Client(build_server(grocy)) as client:
        rows = await text(client, "query_objects", {"entity": "locations", "filters": ["name~frig"]})
        assert rows == '[{"id":2,"name":"Frigo"}]'

        with pytest.raises(ToolError, match="forbidden entity 'api_keys'"):
            await client.call_tool("query_objects", {"entity": "api_keys"})

        assert "4.7.1" in await text(client, "system_info")


@pytest.mark.anyio
async def test_grocy_errors_are_reported() -> None:
    wrong_key = Grocy("http://grocy.test", "wrong", transport=httpx.MockTransport(
        lambda request: httpx.Response(401, json={"error_message": "Unauthorized"})
    ))

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    down = Grocy("http://grocy.test", "key", transport=httpx.MockTransport(refuse))
    for broken, message in ((wrong_key, "Grocy error 401: Unauthorized"),
                            (down, "Grocy is unreachable")):
        async with Client(build_server(broken)) as client:
            with pytest.raises(ToolError, match=message):
                await client.call_tool("stock", {})


@pytest.mark.anyio
async def test_other_account_is_denied(grocy: Grocy) -> None:
    for user in ("player42", None):
        server = build_server(grocy, allowed_user="assistant", current_user=lambda: user)
        async with Client(server) as client:
            with pytest.raises(ToolError, match="Access denied"):
                await client.call_tool("stock", {})
    server = build_server(grocy, allowed_user="assistant", current_user=lambda: "assistant")
    async with Client(server) as client:
        assert await text(client, "stock")
