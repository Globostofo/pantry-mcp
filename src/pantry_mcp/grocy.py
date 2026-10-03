"""Minimal async client for the Grocy REST API (Grocy 4.7)."""

from __future__ import annotations

from typing import Any

import httpx

TIMEOUT_SECONDS = 15


class GrocyError(Exception):
    """Error reported back to the MCP client as a readable message."""


class Grocy:
    def __init__(
        self, base_url: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api",
            headers={"GROCY-API-KEY": api_key, "Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._http.get(path, params=params)
        except httpx.HTTPError as e:
            raise GrocyError(f"Grocy is unreachable: {e}") from e
        if response.is_error:
            raise GrocyError(f"Grocy error {response.status_code}: {error_message(response)}")
        return response.json()

    async def objects(
        self,
        entity: str,
        query: list[str] | tuple[str, ...] = (),
        order: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """`query` items are Grocy filter conditions such as `name~pasta` or `day>=2026-10-03`."""
        params: dict[str, Any] = {}
        if query:
            params["query[]"] = list(query)
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = limit
        return await self.get(f"/objects/{entity}", params)

    async def aclose(self) -> None:
        await self._http.aclose()


def error_message(response: httpx.Response) -> str:
    try:
        return response.json()["error_message"]
    except (ValueError, KeyError, TypeError):
        return response.text[:200] or response.reason_phrase
