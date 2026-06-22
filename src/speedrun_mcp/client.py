"""Thin async client for the speedrun.com REST API (v1).

Read endpoints need no authentication. The identity and write endpoints
(profile, notifications, run submission/moderation) authenticate with a single
``X-API-Key`` header; pass the key to :class:`SpeedrunClient` and it is attached
to every request. The key is never placed in a request body.
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


class AuthError(SpeedrunError):
    """Raised when the API rejects us for a missing/invalid API key.

    speedrun.com answers 403 (not 401) for both a missing and an invalid key.
    """


class SpeedrunClient:
    """Minimal async wrapper around the speedrun.com API.

    One client owns one ``httpx.AsyncClient``; use it as an async context
    manager or remember to ``await close()``.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if api_key:
            # The single auth header for identity/write endpoints. It never goes
            # into a request body, a tool argument, or (see _error_message) a log.
            headers["X-API-Key"] = api_key
        #: Whether an API key was supplied (so callers can fail fast with a clear
        #: message before hitting an endpoint that would 403).
        self.authenticated = bool(api_key)
        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=timeout,
            headers=headers,
            follow_redirects=True,  # abbreviations 30x-redirect to ID-based URLs
            transport=transport,  # injectable for offline tests
        )

    async def __aenter__(self) -> "SpeedrunClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        json: Any | None = None,
    ) -> Any:
        """Send a request and return the full parsed JSON body (incl. pagination).

        GET by default; pass ``method`` / ``json`` for the authenticated write
        endpoints (POST/PUT/DELETE).
        """
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = await self._http.request(method, path, params=clean, json=json)
        except httpx.HTTPError as exc:  # network/DNS/timeout
            raise SpeedrunError(f"Network error talking to speedrun.com: {exc}") from exc

        if resp.status_code == RATE_LIMIT_STATUS:
            raise RateLimitError(
                "speedrun.com rate limit hit (100 requests/minute). Wait a minute and retry."
            )

        if resp.status_code in (401, 403):
            detail = self._error_message(resp)
            msg = (
                f"speedrun.com rejected {method} {path} as unauthenticated "
                f"(HTTP {resp.status_code}); it needs a valid API key."
            )
            if detail:
                msg = f"{msg} speedrun.com says: {detail}"
            raise AuthError(msg)

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

        if not resp.content:  # some write endpoints can answer with an empty body
            return None
        try:
            return resp.json()
        except ValueError as exc:  # non-JSON / empty success body
            raise SpeedrunError(
                f"speedrun.com returned an unparseable response for {path}: {exc}"
            ) from exc

    async def _send(self, method: str, path: str, *, json: Any | None = None) -> Any:
        """Make a write request (POST/PUT/DELETE) and return the ``data`` payload."""
        body = await self._request(path, method=method, json=json)
        if isinstance(body, dict):
            return body.get("data", body)
        return body

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
        if not isinstance(body, dict):
            return None
        message = body.get("message")
        message = message.strip() if isinstance(message, str) and message.strip() else None
        # Run-submission failures attach a list of per-field reasons under
        # ``errors`` (e.g. "[category] is missing and it is required").
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            detail = "; ".join(str(e) for e in errors)
            return f"{message} ({detail})" if message else detail
        return message

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

    async def get_game_records(
        self,
        game: str,
        *,
        top: int = 1,
        scope: str | None = None,
        miscellaneous: bool | None = None,
        embed: str | None = None,
    ) -> list[dict]:
        """A game's leaderboards in one call (GET /games/{id}/records).

        ``top`` caps places per board (1 = world records only). ``scope`` is
        ``full-game`` / ``levels`` / ``all``.
        """
        return await self._get(
            f"/games/{game}/records",
            {"top": top, "scope": scope, "miscellaneous": miscellaneous, "embed": embed},
        )

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

    # -- series ---------------------------------------------------------------

    async def search_series(self, name: str, *, maximum: int = 10) -> list[dict]:
        return await self._get("/series", {"name": name, "max": maximum})

    async def get_series(self, series: str) -> dict:
        return await self._get(f"/series/{series}")

    async def get_series_games(self, series: str, *, maximum: int = 50) -> list[dict]:
        return await self._get(f"/series/{series}/games", {"max": maximum})

    # -- authenticated: identity ----------------------------------------------

    async def get_profile(self) -> dict:
        """The user that owns the API key (GET /profile). Requires auth."""
        return await self._get("/profile")

    async def get_notifications(self, *, direction: str = "desc") -> list[dict]:
        """The authenticated user's notifications, newest first. Requires auth."""
        return await self._get("/notifications", {"orderby": "created", "direction": direction})

    # -- runs: moderation-queue read ------------------------------------------

    async def get_runs(
        self,
        *,
        user: str | None = None,
        guest: str | None = None,
        status: str | None = None,
        game: str | None = None,
        category: str | None = None,
        level: str | None = None,
        examiner: str | None = None,
        orderby: str | None = None,
        direction: str | None = None,
        maximum: int = 20,
        embed: str | None = None,
    ) -> list[dict]:
        """List runs with filters (e.g. ``status='new'`` for the moderation queue,
        or ``user=...`` for a player's submissions).

        A public read — no API key required.
        """
        return await self._get(
            "/runs",
            {
                "user": user,
                "guest": guest,
                "status": status,
                "game": game,
                "category": category,
                "level": level,
                "examiner": examiner,
                "orderby": orderby,
                "direction": direction,
                "max": maximum,
                "embed": embed,
            },
        )

    # -- runs: write / moderation (requires auth) -----------------------------

    async def submit_run(
        self,
        *,
        category: str,
        platform: str,
        times: dict[str, float],
        level: str | None = None,
        date: str | None = None,
        region: str | None = None,
        video: str | None = None,
        comment: str | None = None,
        splitsio: str | None = None,
        emulated: bool | None = None,
        variables: dict[str, dict[str, str]] | None = None,
        players: list[dict[str, str]] | None = None,
    ) -> dict:
        """Submit a run (POST /runs). The body is wrapped in ``{"run": {...}}``.

        ``times`` needs at least one of ``realtime`` / ``realtime_noloads`` /
        ``ingame`` (seconds). ``variables`` is keyed by variable id with
        ``{"type": "pre-defined"|"user-defined", "value": ...}`` values.
        """
        run: dict[str, Any] = {"category": category, "platform": platform, "times": times}
        optional = {
            "level": level,
            "date": date,
            "region": region,
            "video": video,
            "comment": comment,
            "splitsio": splitsio,
            "emulated": emulated,
            "variables": variables,
            "players": players,
        }
        run.update({k: v for k, v in optional.items() if v is not None})
        return await self._send("POST", "/runs", json={"run": run})

    async def set_run_status(self, run_id: str, status: str, *, reason: str | None = None) -> dict:
        """Verify or reject a run (PUT /runs/{id}/status, moderator only).

        Body is double-nested: ``{"status": {"status": ..., "reason": ...}}``.
        A rejection requires a ``reason``.
        """
        inner: dict[str, Any] = {"status": status}
        if reason is not None:
            inner["reason"] = reason
        return await self._send("PUT", f"/runs/{run_id}/status", json={"status": inner})

    async def set_run_players(self, run_id: str, players: list[dict[str, str]]) -> dict:
        """Replace a run's player list (PUT /runs/{id}/players, moderator only).

        The list is a full replacement — include every player, not just additions.
        """
        return await self._send("PUT", f"/runs/{run_id}/players", json={"players": players})

    async def delete_run(self, run_id: str) -> dict:
        """Delete a run (DELETE /runs/{id}). Own runs, or any run for global mods."""
        return await self._send("DELETE", f"/runs/{run_id}")
