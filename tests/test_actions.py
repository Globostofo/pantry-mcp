import re

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from pantry_mcp.grocy import Grocy
from pantry_mcp.server import build_server

from conftest import FakeGrocy


async def call(grocy: Grocy, tool: str, args: dict) -> str:
    async with Client(build_server(grocy)) as client:
        return (await client.call_tool(tool, args)).data


async def fails(grocy: Grocy, tool: str, args: dict, match: str) -> None:
    async with Client(build_server(grocy)) as client:
        with pytest.raises(ToolError, match=match):
            await client.call_tool(tool, args)


def row(fake: FakeGrocy, entity: str, **fields) -> dict:
    return next(r for r in fake.tables[entity] if all(r.get(k) == v for k, v in fields.items()))


@pytest.mark.anyio
async def test_create_product(grocy: Grocy, fake: FakeGrocy) -> None:
    result = await call(grocy, "create_product", {
        "name": "Haricots rouges", "stock_unit": "gramme", "location": "placard",
        "default_best_before_days": 730, "purchase_unit": "boîtes", "purchase_factor": 400,
        "description": "Rouges\n\nen boîte",
    })
    assert result == "Created product Haricots rouges (id 4), stock unit gramme, 1 boîtes = 400 gramme"
    product = row(fake, "products", id=4)
    assert (product["qu_id_stock"], product["qu_id_purchase"], product["qu_id_consume"]) == (1, 3, 1)
    assert product["location_id"] == 1
    assert product["description"] == "<p>Rouges</p><p>en boîte</p>"
    assert row(fake, "quantity_unit_conversions", product_id=4) == {
        "id": 3, "product_id": 4, "from_qu_id": 3, "to_qu_id": 1, "factor": 400,
    }

    await fails(grocy, "create_product", {
        "name": "pâtes", "stock_unit": "gramme", "location": "Placard",
        "default_best_before_days": 365,
    }, r"Product 'Pâtes' already exists \(id 1\)")
    await fails(grocy, "create_product", {
        "name": "Riz", "stock_unit": "gramme", "location": "Placard",
        "default_best_before_days": 365, "purchase_unit": "boîte",
    }, "purchase_factor is required")
    await fails(grocy, "create_product", {
        "name": "Riz", "stock_unit": "litre", "location": "Placard",
        "default_best_before_days": 365,
    }, "Unknown quantity unit 'litre'. Known units: boîte, gramme, pièce")


@pytest.mark.anyio
async def test_create_product_rolls_back_without_conversion(grocy: Grocy, fake: FakeGrocy) -> None:
    fake.failing.add("/objects/quantity_unit_conversions")
    await fails(grocy, "create_product", {
        "name": "Riz", "stock_unit": "gramme", "location": "Placard",
        "default_best_before_days": 365, "purchase_unit": "boîte", "purchase_factor": 500,
    }, "Simulated failure")
    assert all(p["name"] != "Riz" for p in fake.tables["products"])


@pytest.mark.anyio
async def test_create_recipe(grocy: Grocy, fake: FakeGrocy) -> None:
    result = await call(grocy, "create_recipe", {
        "name": "Chili", "servings": 4, "instructions": "Cuire.\nServir chaud.",
        "ingredients": [
            {"product": "Tomates pelées", "amount": 2, "unit": "boîte", "group": "Sauce"},
            {"product": "Œufs", "amount": 3, "note": "optionnel"},
            {"product": "Pâtes", "variable_amount": "à volonté"},
        ],
    })
    assert result == "Created recipe Chili (id 3) with 3 ingredient(s)"
    recipe = row(fake, "recipes", id=3)
    assert (recipe["base_servings"], recipe["desired_servings"]) == (4, 4)
    assert recipe["description"] == "<p>Cuire.</p><p>Servir chaud.</p>"
    positions = [p for p in fake.tables["recipes_pos"] if p["recipe_id"] == 3]
    assert [(p["product_id"], p["amount"], p["qu_id"]) for p in positions] == [
        (3, 800, 3), (2, 3, 2), (1, 0, 1),
    ]
    assert positions[0]["ingredient_group"] == "Sauce"
    assert positions[2]["variable_amount"] == "à volonté"
    assert "note" not in positions[0] and positions[1]["note"] == "optionnel"


@pytest.mark.anyio
async def test_create_recipe_writes_nothing_on_error(grocy: Grocy, fake: FakeGrocy) -> None:
    fake.tables["recipes_pos"] = []
    # Unresolvable ingredient: nothing is written
    await fails(grocy, "create_recipe", {
        "name": "Riz cantonais", "servings": 2,
        "ingredients": [{"product": "Riz", "amount": 200}],
    }, "No product named 'Riz'")
    await fails(grocy, "create_recipe", {
        "name": "Riz cantonais", "servings": 2,
        "ingredients": [{"product": "Œufs", "amount": 100, "unit": "gramme"}],
    }, r"No conversion from 'gramme' for Œufs. Usable units: pièce")
    # Grocy failing halfway: the recipe and its first ingredients are removed
    fake.failing.add("/objects/recipes_pos")
    await fails(grocy, "create_recipe", {
        "name": "Riz cantonais", "servings": 2,
        "ingredients": [{"product": "Œufs", "amount": 2}],
    }, "Simulated failure")
    assert all(r["name"] != "Riz cantonais" for r in fake.tables["recipes"])
    assert fake.tables["recipes_pos"] == []
    await fails(grocy, "create_recipe", {"name": "omelette", "servings": 1, "ingredients": []},
                r"Recipe 'Omelette' already exists \(id 1\)")


@pytest.mark.anyio
async def test_stock_changes_and_undo(grocy: Grocy, fake: FakeGrocy) -> None:
    await fails(grocy, "add_to_stock", {"product": "Tomates pelées", "amount": 2, "unit": "boîte"},
                "has no default best before days: pass best_before_date")
    added = await call(grocy, "add_to_stock", {
        "product": "tomates pelées", "amount": 2, "unit": "boîte",
        "best_before_date": "2027-01-01", "total_price": 2.4,
    })
    assert added == (
        "Added 2 boîtes of Tomates pelées (transaction tx1, undo with `undo`). "
        "Tomates pelées now in stock: 1200 grammes"
    )
    add_call = fake.calls[-2]
    assert add_call[1] == "/stock/products/3/add"
    assert add_call[2] == {
        "amount": 800, "transaction_type": "purchase", "best_before_date": "2027-01-01",
        "price": 0.003,
    }

    assert await call(grocy, "undo", {"transaction_id": "tx1"}) == "Undone transaction tx1"
    assert fake.stock[3]["amount"] == 400

    consumed = await call(grocy, "consume", {"product": "Œufs", "amount": 2})
    assert re.fullmatch(r"Consumed 2 pièces of Œufs \(transaction tx\d.*Œufs now in stock: 4 pièces",
                        consumed)
    await fails(grocy, "consume", {"product": "Œufs", "amount": 10},
                "Amount to be consumed cannot be > current stock amount")
    await fails(grocy, "undo", {"transaction_id": "../system"}, "Invalid transaction id")


@pytest.mark.anyio
async def test_writes_need_exact_names(grocy: Grocy) -> None:
    await fails(grocy, "consume", {"product": "tomates", "amount": 1},
                r"No product named 'tomates'. Similar: Tomates pelées \(id 3\)")
    await fails(grocy, "transfer", {
        "product": "Pâtes", "amount": 1, "from_location": "Placard", "to_location": "frig",
    }, r"No location named 'frig'. Similar: Frigo \(id 2\)")


@pytest.mark.anyio
async def test_shopping_list(grocy: Grocy, fake: FakeGrocy) -> None:
    added = await call(grocy, "shopping_list_add", {"product": "Tomates pelées", "amount": 3, "unit": "boîte"})
    assert added == "Added 3 boîtes of Tomates pelées to the shopping list"
    assert row(fake, "shopping_list", id=99)["amount"] == 1200
    assert await call(grocy, "shopping_list_add", {"note": "Bougies"}) == "Added 'Bougies' to the shopping list"
    assert row(fake, "shopping_list", note="Bougies")["shopping_list_id"] == 1
    await fails(grocy, "shopping_list_add", {}, "Give a product, or a note")


@pytest.mark.anyio
async def test_plan_meal_and_cook(grocy: Grocy, fake: FakeGrocy) -> None:
    planned = await call(grocy, "plan_meal", {
        "day": "2026-10-10", "recipe": "Omelette", "servings": 3, "section": "dîner",
    })
    assert planned == "Planned Omelette (3 servings) on 2026-10-10 (meal_plan id 5)"
    assert row(fake, "meal_plan", id=5) == {
        "id": 5, "day": "2026-10-10", "type": "recipe", "recipe_id": 1, "recipe_servings": 3,
        "section_id": 2,
    }
    product = await call(grocy, "plan_meal", {"day": "2026-10-11", "product": "Tomates pelées", "unit": "boîte"})
    assert product == "Planned Tomates pelées: 1 boîte on 2026-10-11 (meal_plan id 6)"
    await fails(grocy, "plan_meal", {"day": "2026-10-10", "recipe": "Omelette", "product": "Pâtes"},
                "exactly one of recipe, product or note")
    await fails(grocy, "plan_meal", {"day": "samedi", "note": "x"}, "Invalid date")

    cooked = await call(grocy, "cook_recipe", {"recipe": "Omelette", "servings": 4})
    assert cooked == "Consumed the in-stock ingredients of Omelette for 4 servings"
    assert ("PUT", "/objects/recipes/1", {"desired_servings": 4}) in fake.calls
    assert ("POST", "/recipes/1/consume", {}) in fake.calls


@pytest.mark.anyio
async def test_generic_objects(grocy: Grocy, fake: FakeGrocy) -> None:
    assert await call(grocy, "create_object", {"entity": "locations", "data": {"name": "Congélateur"}}) \
        == "Created locations id 3"
    assert await call(grocy, "update_object", {
        "entity": "quantity_units", "object_id": 2, "data": {"name": "pièce entière"},
    }) == "Updated quantity_units id 2"
    assert row(fake, "quantity_units", id=2)["name_plural"] == "pièces"
    assert await call(grocy, "delete_object", {"entity": "locations", "object_id": 3}) \
        == "Deleted locations id 3"
    for entity in ("stock", "api_keys", "stock_log"):
        await fails(grocy, "delete_object", {"entity": entity, "object_id": 1},
                    f"Entity '{entity}' cannot be written")


@pytest.mark.anyio
async def test_writes_check_the_account(grocy: Grocy, fake: FakeGrocy) -> None:
    server = build_server(grocy, allowed_user="assistant", current_user=lambda: "player42")
    async with Client(server) as client:
        with pytest.raises(ToolError, match="Access denied"):
            await client.call_tool("create_object", {"entity": "locations", "data": {"name": "x"}})
    assert fake.calls == []
