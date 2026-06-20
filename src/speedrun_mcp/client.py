"""Thin async client for the speedrun.com REST API (v1).

The public API requires no authentication for the read endpoints used here.
Docs: https://github.com/speedruncomorg/api/tree/master/version1
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

import httpx

API_BASE = "https://www.speedrun.com/api/v1"

try:
    _VERSION = version("speedrun-mcp")
except PackageNotFoundError:  # running from a source checkout without install
    _VERSION = "0.0.0+dev"

USER_AGENT = f"speedrun-mcp/{_VERSION} (+https://github.com/williamcodes/speedrun-mcp)"

# speedrun.com allows 100 requests/min/IP and answers 420 when exceeded.
RATE_LIMIT_STATUS = 420


class SpeedrunError(RuntimeError):
    """Raised when the speedrun.com API returns an error we can explain."""


class RateLimitError(SpeedrunError):
    """Raised when the API rejects us for exceeding 100 requests/minute."""


class NotFoundError(SpeedrunError):
    """Raised when the API returns HTTP 404 for a resource (bad id/filters)."""


class SpeedrunClient:
    """Minimal async wrapper around the speedrun.com API.

    One client owns one ``httpx.AsyncClient``; use it as an async context
    manager or remember to ``await close()``.
    """

    def __init__(self, *, timeout: float = 20.0) -> None:
        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,  # abbreviations 30x-redirect to ID-based URLs
        )

    async def __aenter__(self) -> "SpeedrunClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a path and return the full parsed JSON body (incl. pagination)."""
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = await self._http.get(path, params=clean)
        except httpx.HTTPError as exc:  # network/DNS/timeout
            raise SpeedrunError(f"Network error talking to speedrun.com: {exc}") from exc

        if resp.status_code == RATE_LIMIT_STATUS:
            raise RateLimitError(
                "speedrun.com rate limit hit (100 requests/minute). Wait a minute and retry."
            )

        if resp.status_code == 404:
            detail = self._error_message(resp)
            msg = f"Not found: {path} (check the id/abbreviation and any filters)."
            if detail:
                msg = f"{msg} speedrun.com says: {detail}"
            raise NotFoundError(msg)

        if resp.status_code >= 400:
            detail = self._error_message(resp)
            msg = f"speedrun.com returned HTTP {resp.status_code} for {path}."
            if detail:
                msg = f"{msg} speedrun.com says: {detail}"
            raise SpeedrunError(msg)

        try:
            return resp.json()
        except ValueError as exc:  # non-JSON / empty success body
            raise SpeedrunError(
                f"speedrun.com returned an unparseable response for {path}: {exc}"
            ) from exc

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a path and return the parsed ``data`` payload.

        speedrun.com wraps successful responses in ``{"data": ...}``; we unwrap
        it so callers never have to. Pagination metadata is dropped on purpose —
        for single-page tools that cap their own result counts. Use
        :meth:`_get_paginated` for collections that may exceed one page.
        """
        body = await self._request(path, params)
        if not isinstance(body, dict):
            return body
        return body.get("data", body)

    async def _get_paginated(self, path: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Fetch and concatenate ALL pages of a collection endpoint.

        Some collections (e.g. ``/platforms``, ~235 items) exceed the 200/page
        cap, so a single request silently truncates. This walks every page by
        following the ``pagination.links`` ``next`` marker / incrementing offset.
        """
        merged = dict(params or {})
        page_size = int(merged.get("max") or 200)
        merged["max"] = page_size
        collected: list[dict] = []
        offset = 0
        while True:
            merged["offset"] = offset
            body = await self._request(path, merged)
            data = body.get("data", []) if isinstance(body, dict) else (body or [])
            collected.extend(data)
            pagination = body.get("pagination", {}) if isinstance(body, dict) else {}
            has_next = any(link.get("rel") == "next" for link in pagination.get("links", []))
            if not has_next or len(data) < page_size:  # last (or short/empty) page
                break
            offset += page_size
        return collected

    @staticmethod
    def _error_message(resp: httpx.Response) -> str | None:
        """Best-effort extraction of the ``message`` field from an error body.

        speedrun.com error bodies look like
        ``{"status":400,"message":"...","links":[...]}``. A non-JSON or empty
        body must not raise here — we just return ``None`` so the caller can
        fall back to a generic message.
        """
        try:
            body = resp.json()
        except ValueError:
            return None
        if isinstance(body, dict):
            message = body.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return None

    # -- games ----------------------------------------------------------------

    async def search_games(self, name: str, *, maximum: int = 10) -> list[dict]:
        return await self._get("/games", {"name": name, "max": maximum})

    async def get_game(self, game: str, *, embed: str | None = None) -> dict:
        return await self._get(f"/games/{game}", {"embed": embed})

    async def get_categories(self, game: str) -> list[dict]:
        return await self._get(f"/games/{game}/categories")

    async def get_levels(self, game: str) -> list[dict]:
        return await self._get(f"/games/{game}/levels")

    async def get_game_variables(self, game: str) -> list[dict]:
        return await self._get(f"/games/{game}/variables")

    async def get_category_variables(self, category: str) -> list[dict]:
        return await self._get(f"/categories/{category}/variables")

    # -- leaderboards ---------------------------------------------------------

    async def get_leaderboard(
        self,
        game: str,
        category: str,
        *,
        level: str | None = None,
        top: int | None = None,
        variables: dict[str, str] | None = None,
        platform: str | None = None,
        region: str | None = None,
        timing: str | None = None,
        emulators: bool | None = None,
        date: str | None = None,
        embed: str | None = None,
    ) -> dict:
        if level:
            path = f"/leaderboards/{game}/level/{level}/{category}"
        else:
            path = f"/leaderboards/{game}/category/{category}"
        params: dict[str, Any] = {
            "top": top,
            "platform": platform,
            "region": region,
            "timing": timing,
            "emulators": emulators,
            "date": date,
            "embed": embed,
        }
        for var_id, value_id in (variables or {}).items():
            params[f"var-{var_id}"] = value_id
        return await self._get(path, params)

    # -- users / runs ---------------------------------------------------------

    async def search_users(self, name: str, *, maximum: int = 10) -> list[dict]:
        # 'name' does fuzzy/partial matching; 'lookup' is exact-only.
        return await self._get("/users", {"name": name, "max": maximum})

    async def get_user(self, user: str) -> dict:
        return await self._get(f"/users/{user}")

    async def get_user_personal_bests(
        self, user: str, *, embed: str | None = None
    ) -> list[dict]:
        return await self._get(f"/users/{user}/personal-bests", {"embed": embed})

    async def get_run(self, run_id: str, *, embed: str | None = None) -> dict:
        return await self._get(f"/runs/{run_id}", {"embed": embed})

    # -- platforms / regions --------------------------------------------------

    async def get_platforms(self) -> list[dict]:
        return await self._get_paginated("/platforms")

    async def get_regions(self) -> list[dict]:
        return await self._get_paginated("/regions")
