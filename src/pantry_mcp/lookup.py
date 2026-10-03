"""Name resolution and quantity unit conversions shared by reads and writes.

Grocy stores every amount (stock, recipe ingredients, shopping list, meal plan) in the
product's stock quantity unit; a `qu_id` next to an amount is only the unit to display it
in, converted with `quantity_unit_conversions_resolved`, as Grocy's own UI does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pantry_mcp.grocy import Grocy, GrocyError


def num(value: Any) -> str:
    f = float(value)
    return str(int(f)) if f.is_integer() else f"{f:.3f}".rstrip("0").rstrip(".")


def by_id(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(r["id"]): r for r in rows}


async def find(
    grocy: Grocy, entity: str, name_or_id: str, extra: tuple[str, ...] = (), exact: bool = False
) -> dict[str, Any]:
    """One object by id or by name, ignoring case. Reads also accept a unique partial name;
    writes pass `exact=True` so that "lait" never silently targets "lait de coco".
    Names are compared in Python: SQLite's LIKE only ignores case for ASCII letters."""
    key = name_or_id.strip()
    label = entity.removesuffix("s")
    if key.isdigit():
        rows = await grocy.objects(entity, (f"id={key}", *extra))
        if not rows:
            raise GrocyError(f"No {label} with id {key}")
        return rows[0]
    rows = await grocy.objects(entity, extra)
    exact_rows = [r for r in rows if r["name"].strip().casefold() == key.casefold()]
    if len(exact_rows) == 1:
        return exact_rows[0]
    partial = [r for r in rows if key.casefold() in r["name"].casefold()]
    names = ", ".join(f"{r['name']} (id {r['id']})" for r in partial[:20])
    if exact or not partial:
        hint = f". Similar: {names}" if names else ""
        raise GrocyError(f"No {label} named {name_or_id!r}{hint}")
    if len(partial) > 1:
        raise GrocyError(f"Several {label}s match {name_or_id!r}: {names}")
    return partial[0]


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
            return unit["name"].strip()
        return (unit.get("name_plural") or unit["name"]).strip()

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

    def unit_id(self, name: str) -> int:
        key = name.strip().casefold()
        for unit in self.units.values():
            names = {unit["name"].strip().casefold(), (unit.get("name_plural") or "").strip().casefold()}
            if key and key in names:
                return int(unit["id"])
        known = ", ".join(sorted(u["name"].strip() or f"(blank, id {u['id']})" for u in self.units.values()))
        raise GrocyError(f"Unknown quantity unit {name!r}. Known units: {known}")

    def usable_units(self, product_id: int) -> list[str]:
        stock_qu = int(self.products[product_id]["qu_id_stock"])
        ids = [stock_qu] + [to for (pid, frm, to) in self.factors if pid == product_id and frm == stock_qu]
        return [self.units[i]["name"].strip() for i in dict.fromkeys(ids) if i in self.units]

    def to_stock(self, product_id: Any, amount: float, unit: str | None = None) -> tuple[float, int]:
        """Convert `amount` expressed in `unit` (default: stock unit) to the product's stock
        unit. Returns the stock amount and the unit id to display it in."""
        pid = int(product_id)
        stock_qu = int(self.products[pid]["qu_id_stock"])
        if not unit:
            return float(amount), stock_qu
        qu = self.unit_id(unit)
        if qu == stock_qu:
            return float(amount), stock_qu
        factor = self.factors.get((pid, qu, stock_qu))
        if factor is None:
            raise GrocyError(
                f"No conversion from {unit!r} for {self.products[pid]['name']}. "
                f"Usable units: {', '.join(self.usable_units(pid))} "
                "(add a quantity_unit_conversions object to allow another one)"
            )
        return float(amount) * factor, qu


async def load_units(grocy: Grocy) -> Units:
    units = await grocy.objects("quantity_units")
    products = await grocy.objects("products")
    conversions = await grocy.objects("quantity_unit_conversions_resolved")
    factors = {
        (int(c["product_id"]), int(c["from_qu_id"]), int(c["to_qu_id"])): float(c["factor"])
        for c in conversions
        if c.get("product_id")
    }
    return Units(by_id(units), by_id(products), factors)
