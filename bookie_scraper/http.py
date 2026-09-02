from __future__ import annotations

from typing import Any

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=20.0)


async def fetch_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    resp = await client.get(url, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


async def fetch_json_ok(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    resp = await client.get(url, headers=headers, params=params)
    data = None
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype or (resp.content[:1] in (b"{", b"[")):
        try:
            data = resp.json()
        except Exception:
            data = None
    return resp.status_code, data
