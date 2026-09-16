"""Thin async client for the speedrun.com REST API (v1).

Read endpoints need no authentication. The identity and write endpoints
(profile, notifications, run submission/moderation) authenticate with a single
``X-API-Key`` header; pass the key to :class:`SpeedrunClient` and it is attached
to every request. The key is never placed in a request body.
Docs: https://github.com/speedruncomorg/api/tree/master/version1
"""

from __future__ import annotations

import math
import unicodedata
from datetime import date as _date
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from urllib.parse import parse_qs, urlsplit

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


def _require_nonblank(value: str, field: str) -> str:
    """Reject an empty/whitespace required value before making an API request."""
    if not value or not value.strip():
        raise ValueError(f"{field} must be a non-empty value.")
    return value


def _require_searchable_name(name: str) -> None:
    """Do not let an ignored game/series query become an unfiltered collection."""
    _require_nonblank(name, "name")
    # The API searches Latin letters (including accents) and ASCII digits, but
    # drops queries made solely of Japanese/Cyrillic text, symbols or punctuation.
    if not any(
        unicodedata.name(char, "").startswith("LATIN ") or "0" <= char <= "9" for char in name
    ):
        raise ValueError(
            "name must contain a Latin letter or ASCII digit for speedrun.com search. "
            "Use the international or romanized title; unsupported queries can return "
            "an unfiltered list."
        )


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

    async def __aenter__(self) -> SpeedrunClient:
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
            if method != "GET":
                raise SpeedrunError(
                    f"Write outcome unknown for {method} {path}: {exc}. "
                    "The action may have succeeded. Check current state before retrying; "
                    "do not repeat the write automatically."
                ) from exc
            raise SpeedrunError(f"Network error talking to speedrun.com: {exc}") from exc

        self._raise_for_status(resp, method=method, path=path)

        if not resp.content:  # some write endpoints can answer with an empty body
            return None
        try:
            return resp.json()
        except ValueError as exc:  # non-JSON / empty success body
            if method != "GET":
                location = resp.headers.get("Location")
                raise SpeedrunError(
                    f"speedrun.com acknowledged {method} {path} with HTTP {resp.status_code}, "
                    "but its response could not be parsed. The action may have succeeded. "
                    "Check current state before retrying; do not repeat the write automatically."
                    + (f" Resource location: {location}" if location else "")
                ) from exc
            raise SpeedrunError(
                f"speedrun.com returned an unparseable response for {path}: {exc}"
            ) from exc

    def _raise_for_status(self, resp: httpx.Response, *, method: str, path: str) -> None:
        """Map HTTP failures to client errors, including any API validation details."""
        if resp.status_code < 400:
            return
        if resp.status_code == RATE_LIMIT_STATUS:
            raise RateLimitError(
                "speedrun.com rate limit hit (100 requests/minute). Wait a minute and retry."
            )

        error: type[SpeedrunError]
        if resp.status_code in (401, 403):
            # 403 covers both a missing/invalid key AND a valid key without
            # permission (e.g. a non-moderator calling verify/reject), so don't
            # assert the key is bad — the surfaced detail disambiguates.
            error = AuthError
            msg = (
                f"speedrun.com rejected {method} {path} (HTTP {resp.status_code}); "
                "your API key may be missing, invalid, or lack permission for this action."
            )
        elif resp.status_code == 404:
            error = NotFoundError
            msg = f"Not found: {path} (check the id/abbreviation and any filters)."
        else:
            error = SpeedrunError
            msg = f"speedrun.com returned HTTP {resp.status_code} for {path}."

        detail = self._error_message(resp)
        if detail:
            msg = f"{msg} speedrun.com says: {detail}"
        if method != "GET" and resp.status_code >= 500:
            msg += (
                " Write outcome unknown. Check current state before retrying; "
                "do not repeat the write automatically."
            )
        raise error(msg)

    async def _send(self, method: str, path: str, *, json: Any | None = None) -> Any:
        """Make a write request (POST/PUT/DELETE) and return the ``data`` payload."""
        body = await self._request(path, method=method, json=json)
        if isinstance(body, dict):
            return body.get("data", body)
        return body

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a path and return the parsed ``data`` payload.

        speedrun.com wraps successful responses in ``{"data": ...}``; we unwrap
        it so callers never have to. Use :meth:`_get_page` for capped collections
        or :meth:`_get_paginated` to consume a complete collection.
        """
        body = await self._request(path, params)
        if not isinstance(body, dict):
            return body
        return body.get("data", body)

    async def _get_page(self, path: str, params: dict[str, Any]) -> dict:
        """Preserve page boundaries without claiming a collection's total size."""
        body = await self._request(path, params)
        data = body["data"]
        pagination = body.get("pagination")
        offset = int((pagination or {}).get("offset", params.get("offset", 0)))
        limit = int((pagination or {}).get("max", params.get("max", 20)))
        links = (pagination or {}).get("links", [])
        next_link = next((link for link in links if link.get("rel") == "next"), None)
        has_more = bool(next_link) if pagination is not None else None
        next_offset = None
        if next_link is not None:
            query = parse_qs(urlsplit(next_link.get("uri", "")).query)
            next_offset = int(query.get("offset", [offset + len(data)])[0])
            if next_offset <= offset:
                raise SpeedrunError(
                    f"Pagination did not advance for {path}; refusing to repeat a page."
                )
        elif has_more is None and data:
            next_offset = offset + len(data)
        return {
            "data": data,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
            "next_offset": next_offset,
        }

    async def _get_paginated(self, path: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Fetch and concatenate ALL pages of a collection endpoint.

        Some collections (e.g. ``/platforms``, ~235 items) exceed the 200/page
        cap, so a single request silently truncates. This walks every page by
        following the ``pagination.links`` ``next`` marker / incrementing offset.
        """
        merged = dict(params or {})
        # The API hard-caps every page at 200.
        page_size = min(int(merged.get("max") or 200), 200)
        merged["max"] = page_size
        collected: list[dict] = []
        offset = 0
        while True:
            merged["offset"] = offset
            page = await self._get_page(path, merged)
            collected.extend(page["data"])
            if page["has_more"] is None:
                raise SpeedrunError(
                    f"Missing pagination metadata for {path}; completeness is unknown."
                )
            if not page["has_more"]:
                break
            offset = page["next_offset"]
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

    async def search_games(self, name: str, *, maximum: int = 10, offset: int = 0) -> dict:
        _require_searchable_name(name)
        return await self._get_page("/games", {"name": name, "max": maximum, "offset": offset})

    async def get_game(self, game: str, *, embed: str | None = None) -> dict:
        _require_nonblank(game, "game")
        return await self._get(f"/games/{game}", {"embed": embed})

    async def get_categories(self, game: str) -> list[dict]:
        _require_nonblank(game, "game")
        return await self._get(f"/games/{game}/categories")

    async def get_levels(self, game: str) -> list[dict]:
        _require_nonblank(game, "game")
        return await self._get(f"/games/{game}/levels")

    async def get_game_variables(self, game: str) -> list[dict]:
        _require_nonblank(game, "game")
        return await self._get(f"/games/{game}/variables")

    async def get_category_variables(self, category: str) -> list[dict]:
        _require_nonblank(category, "category")
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
        """Fetch every page of a game's leaderboards (GET /games/{id}/records).

        ``top`` caps places per board (1 = world records only). ``scope`` is
        ``full-game`` / ``levels`` / ``all``.
        """
        _require_nonblank(game, "game")
        return await self._get_paginated(
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
        _require_nonblank(game, "game")
        _require_nonblank(category, "category")
        if date is not None:
            try:
                parsed_date = _date.fromisoformat(date)
            except ValueError as exc:
                raise ValueError(f"date must be in YYYY-MM-DD form, got {date!r}.") from exc
            if parsed_date.isoformat() != date:
                raise ValueError(f"date must be in YYYY-MM-DD form, got {date!r}.")
        if level is not None:
            _require_nonblank(level, "level")
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
        leaderboard = await self._get(path, params)
        applied = leaderboard.get("values") or {}
        ignored = [
            var_id
            for var_id, value_id in (variables or {}).items()
            if applied.get(var_id) != value_id
        ]
        if ignored:
            raise ValueError(
                "Leaderboard did not apply the requested variable filters: "
                f"{', '.join(ignored)}. Use list_variables to find valid ids and values."
            )
        return leaderboard

    # -- users / runs ---------------------------------------------------------

    async def search_users(self, name: str, *, maximum: int = 10, offset: int = 0) -> dict:
        _require_nonblank(name, "name")
        # 'name' does fuzzy/partial matching; 'lookup' is exact-only.
        return await self._get_page("/users", {"name": name, "max": maximum, "offset": offset})

    async def get_user(self, user: str) -> dict:
        _require_nonblank(user, "user")
        return await self._get(f"/users/{user}")

    async def get_user_personal_bests(self, user: str, *, embed: str | None = None) -> list[dict]:
        _require_nonblank(user, "user")
        return await self._get(f"/users/{user}/personal-bests", {"embed": embed})

    async def get_run(self, run_id: str, *, embed: str | None = None) -> dict:
        _require_nonblank(run_id, "run_id")
        return await self._get(f"/runs/{run_id}", {"embed": embed})

    # -- platforms / regions --------------------------------------------------

    async def get_platforms(self) -> list[dict]:
        return await self._get_paginated("/platforms")

    async def get_regions(self) -> list[dict]:
        return await self._get_paginated("/regions")

    # -- series ---------------------------------------------------------------

    async def search_series(self, name: str, *, maximum: int = 10, offset: int = 0) -> dict:
        _require_searchable_name(name)
        return await self._get_page("/series", {"name": name, "max": maximum, "offset": offset})

    async def get_series(self, series: str) -> dict:
        _require_nonblank(series, "series")
        return await self._get(f"/series/{series}")

    async def get_series_games(self, series: str, *, maximum: int = 50, offset: int = 0) -> dict:
        _require_nonblank(series, "series")
        return await self._get_page(f"/series/{series}/games", {"max": maximum, "offset": offset})

    # -- authenticated: identity ----------------------------------------------

    async def get_profile(self) -> dict:
        """The user that owns the API key (GET /profile). Requires auth."""
        return await self._get("/profile")

    async def get_notifications(
        self, *, direction: str = "desc", maximum: int = 20, offset: int = 0
    ) -> dict:
        """The authenticated user's notifications, newest first. Requires auth."""
        return await self._get_page(
            "/notifications",
            {"orderby": "created", "direction": direction, "max": maximum, "offset": offset},
        )

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
        offset: int = 0,
    ) -> dict:
        """List runs with filters (e.g. ``status='new'`` for the moderation queue,
        or ``user=...`` for a player's submissions).

        A public read — no API key required.
        """
        # Unlike resource paths, /runs query filters do not resolve names or
        # abbreviations. Resolve every supplied reference, including id-shaped
        # usernames, rather than guessing whether an eight-character string is an id.
        references = {
            "user": ("users", user),
            "game": ("games", game),
            "category": ("categories", category),
            "level": ("levels", level),
            "examiner": ("users", examiner),
        }
        for field, (_, value) in references.items():
            if value is not None:
                _require_nonblank(value, field)
        filters = {}
        for field, (resource, value) in references.items():
            if value is not None:
                record = await self._get(f"/{resource}/{value}")
                filters[field] = record["id"]
        return await self._get_page(
            "/runs",
            {
                **filters,
                "guest": guest,
                "status": status,
                "orderby": orderby,
                "direction": direction,
                "max": maximum,
                "offset": offset,
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
        if not times:
            raise ValueError("times needs at least one of realtime / realtime_noloads / ingame.")
        _require_nonblank(category, "category")
        _require_nonblank(platform, "platform")
        # Reject universally-invalid input locally for a clear, cheap error; the
        # API remains the source of truth for game-specific rules.
        for time_name, seconds in times.items():
            if not isinstance(seconds, int | float) or not math.isfinite(seconds) or seconds <= 0:
                raise ValueError(
                    f"time '{time_name}' must be a positive, finite number of seconds."
                )
        if date is not None:
            try:
                _date.fromisoformat(date)
            except ValueError as exc:
                raise ValueError(f"date must be in YYYY-MM-DD form, got {date!r}.") from exc
        if video is not None and not video.startswith(("http://", "https://")):
            raise ValueError("video must be an http(s) URL.")
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
        _require_nonblank(run_id, "run_id")
        if status == "rejected" and not (reason and reason.strip()):
            raise ValueError("Rejecting a run requires a non-empty reason.")
        inner: dict[str, Any] = {"status": status}
        if reason is not None:
            inner["reason"] = reason
        return await self._send("PUT", f"/runs/{run_id}/status", json={"status": inner})

    async def set_run_players(self, run_id: str, players: list[dict[str, str]]) -> dict:
        """Replace a run's player list (PUT /runs/{id}/players, moderator only).

        The list is a full replacement — include every player, not just additions.
        """
        _require_nonblank(run_id, "run_id")
        return await self._send("PUT", f"/runs/{run_id}/players", json={"players": players})

    async def delete_run(self, run_id: str) -> dict:
        """Delete a run (DELETE /runs/{id}). Own runs, or any run for global mods."""
        _require_nonblank(run_id, "run_id")
        return await self._send("DELETE", f"/runs/{run_id}")
