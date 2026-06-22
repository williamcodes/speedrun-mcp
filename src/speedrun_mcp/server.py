"""MCP server exposing speedrun.com data as tools.

Typical flow for a model:
  1. ``search_games`` to turn a title into a game id.
  2. ``list_categories`` (and ``list_variables`` for subcategories) to pick a
     category/filters.
  3. ``get_leaderboard`` / ``get_world_record`` for the actual rankings.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from . import format as fmt
from .client import SpeedrunClient


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """Close the shared HTTP client when the server shuts down (L8)."""
    try:
        yield
    finally:
        global _client
        if _client is not None:
            await _client.close()
            _client = None


mcp = FastMCP(
    "speedrun",
    instructions=(
        "Query speedrun.com: games, categories, leaderboards, world records, "
        "players and their personal bests. Resolve a game title to an id with "
        "search_games first, then use list_categories / list_variables to find "
        "the category and subcategory filters a leaderboard needs."
    ),
    lifespan=_lifespan,
)

# A single shared client for the process lifetime. Created lazily so importing
# this module (e.g. for tests) never opens a socket.
_client: SpeedrunClient | None = None


def _get_client() -> SpeedrunClient:
    global _client
    if _client is None:
        # The API key (if any) comes only from the environment — never from a
        # tool argument, so it can't leak into the model's context or logs.
        _client = SpeedrunClient(api_key=os.environ.get("SPEEDRUN_API_KEY") or None)
    return _client


def _require_auth() -> SpeedrunClient:
    """Return the client, or raise a clear error if no API key is configured."""
    client = _get_client()
    if not client.authenticated:
        raise RuntimeError(
            "This tool needs a speedrun.com API key. Set the SPEEDRUN_API_KEY "
            "environment variable (get your key at https://www.speedrun.com/api/auth)."
        )
    return client


def _require_writes() -> None:
    """Guard write tools: raise a clear, actionable error when writes are off.

    This is what makes read-only mode discoverable — calling a write tool without
    ``SPEEDRUN_ENABLE_WRITES`` set explains exactly how to switch to read-write.
    """
    if not WRITES_ENABLED:
        raise RuntimeError(
            "This server is in read-only mode, so this write action is disabled. "
            "To allow run submission and moderation, set the environment variable "
            "SPEEDRUN_ENABLE_WRITES=1 (alongside SPEEDRUN_API_KEY) and restart the "
            "server."
        )


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


# Whether an API key is configured. All authenticated tools — identity reads
# AND the write tools — are exposed only when a key is present, so a keyless
# user sees just the public read tools. The key is entirely opt-in.
AUTH_ENABLED = bool(os.environ.get("SPEEDRUN_API_KEY"))

# Whether writes are *armed*. A key alone gives read-only behaviour: the write
# tools are still listed (so they're discoverable and the model can learn how to
# enable them), but each refuses to run until this flag is set. Read at import:
# the MCP client sets env before launching the server.
WRITES_ENABLED = _truthy(os.environ.get("SPEEDRUN_ENABLE_WRITES"))


def _read_anno(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=True, openWorldHint=True)


def _write_anno(
    title: str, *, destructive: bool = False, idempotent: bool = False
) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=True,
    )


def _auth_tool(**kwargs):
    """Like ``@mcp.tool`` but only registers when an API key is configured.

    Used for every authenticated tool (identity reads and the write tools), so
    keyless users see only the public reads. Write tools add ``_require_writes``
    on top to stay read-only until writes are armed; the function is defined on
    the module either way.
    """

    def decorator(fn):
        return mcp.tool(**kwargs)(fn) if AUTH_ENABLED else fn

    return decorator


@mcp.tool(annotations=_read_anno("Search games"))
async def search_games(
    name: Annotated[str, Field(description="Game title or partial title to search for.")],
    limit: Annotated[int, Field(ge=1, le=50, description="Max games to return.")] = 10,
) -> list[dict]:
    """Fuzzy-search games by name. Returns ids, abbreviations and release years.

    Use the returned ``id`` (or ``abbreviation``) with the other tools.
    """
    games = await _get_client().search_games(name, maximum=limit)
    return [fmt.game_summary(g) for g in games]


@mcp.tool(annotations=_read_anno("Get game"))
async def get_game(
    game: Annotated[str, Field(description="Game id or abbreviation (e.g. 'sm64' or 'o1y9wo6q').")],
    include_levels: Annotated[
        bool, Field(description="Also include individual levels (for IL leaderboards).")
    ] = False,
) -> dict:
    """Get a game's details plus its categories (and optionally its levels).

    The embedded categories give you the ``category_id`` needed for
    ``get_leaderboard``.
    """
    embed = "categories,levels" if include_levels else "categories"
    g = await _get_client().get_game(game, embed=embed)
    out = fmt.game_summary(g)
    out["categories"] = [
        fmt.category_summary(c) for c in (g.get("categories") or {}).get("data", [])
    ]
    if include_levels:
        out["levels"] = [
            {"id": lv.get("id"), "name": lv.get("name")}
            for lv in (g.get("levels") or {}).get("data", [])
        ]
    return out


@mcp.tool(annotations=_read_anno("List categories"))
async def list_categories(
    game: Annotated[str, Field(description="Game id or abbreviation.")],
) -> list[dict]:
    """List a game's categories (e.g. 'Any%', '120 Star'), with their ids and rules."""
    cats = await _get_client().get_categories(game)
    return [fmt.category_summary(c) for c in cats]


@mcp.tool(annotations=_read_anno("List variables"))
async def list_variables(
    game: Annotated[str, Field(description="Game id or abbreviation.")],
) -> list[dict]:
    """List a game's variables — the subcategories and filters a leaderboard accepts.

    Each variable has an id and a ``values`` map of {value_id: label}. Pass these
    to ``get_leaderboard``/``get_world_record`` as ``variables={variable_id: value_id}``
    to target a specific subcategory (e.g. '16 Star', difficulty 'Hard').
    """
    variables = await _get_client().get_game_variables(game)
    return [fmt.variable_summary(v) for v in variables]


@mcp.tool(annotations=_read_anno("Get leaderboard"))
async def get_leaderboard(
    game: Annotated[str, Field(description="Game id or abbreviation.")],
    category: Annotated[str, Field(description="Category id or abbreviation.")],
    top: Annotated[int, Field(ge=1, le=200, description="Return the top N places.")] = 10,
    level: Annotated[
        str | None, Field(description="Level id for an individual-level (IL) leaderboard.")
    ] = None,
    variables: Annotated[
        dict[str, str] | None,
        Field(description="Subcategory/variable filters as {variable_id: value_id}."),
    ] = None,
    platform: Annotated[str | None, Field(description="Platform id to filter by.")] = None,
    region: Annotated[str | None, Field(description="Region id to filter by.")] = None,
    timing: Annotated[
        str | None,
        Field(description="Sort by 'realtime', 'realtime_noloads', or 'ingame'."),
    ] = None,
    emulators: Annotated[
        bool | None, Field(description="True = emulators only, False = real devices only.")
    ] = None,
    date: Annotated[
        str | None, Field(description="ISO date; only runs on or before this date.")
    ] = None,
) -> dict:
    """Get a ranked leaderboard for a game/category (full-game or individual level).

    Players, subcategory labels and the category name are resolved for you. For
    subcategory filters, discover ids with ``list_variables`` first.
    """
    lb = await _get_client().get_leaderboard(
        game,
        category,
        level=level,
        top=top,
        variables=variables,
        platform=platform,
        region=region,
        timing=timing,
        emulators=emulators,
        date=date,
        embed="game,players,variables,category",
    )
    return fmt.leaderboard_view(lb)


@mcp.tool(annotations=_read_anno("Get world record"))
async def get_world_record(
    game: Annotated[str, Field(description="Game id or abbreviation.")],
    category: Annotated[str, Field(description="Category id or abbreviation.")],
    level: Annotated[str | None, Field(description="Level id for an IL world record.")] = None,
    variables: Annotated[
        dict[str, str] | None,
        Field(description="Subcategory/variable filters as {variable_id: value_id}."),
    ] = None,
) -> dict:
    """Get the current world record (place 1) for a game/category.

    A convenience wrapper over ``get_leaderboard``. Returns the leaderboard
    metadata plus ``world_record`` (the single fastest run, or ``None`` if the
    leaderboard is empty) and ``tied`` (any other runs also at place 1; usually
    empty).
    """
    lb = await _get_client().get_leaderboard(
        game,
        category,
        level=level,
        top=1,
        variables=variables,
        embed="game,players,variables,category",
    )
    view = fmt.leaderboard_view(lb)
    runs = view.pop("runs", [])
    view["world_record"] = runs[0] if runs else None
    view["tied"] = [r for r in runs[1:] if r.get("place") == 1]
    return view


@mcp.tool(annotations=_read_anno("Search users"))
async def search_users(
    name: Annotated[str, Field(description="Username (or partial) to look up.")],
    limit: Annotated[int, Field(ge=1, le=50, description="Max users to return.")] = 10,
) -> list[dict]:
    """Search for speedrun.com users by name. Returns ids, countries and signup dates."""
    users = await _get_client().search_users(name, maximum=limit)
    return [fmt.user_summary(u) for u in users]


@mcp.tool(annotations=_read_anno("Get personal bests"))
async def get_user_personal_bests(
    user: Annotated[str, Field(description="User id or exact username.")],
    limit: Annotated[int, Field(ge=1, le=200, description="Max personal bests to return.")] = 25,
) -> dict:
    """Get a player's personal best runs across all games, with game/category names.

    ``/personal-bests`` is unpaginated, so ``total_available`` is the player's true
    PB count and ``returned`` is how many came back after applying ``limit``.
    """
    pbs = await _get_client().get_user_personal_bests(user, embed="game,category,players")
    entries = []
    for item in pbs[:limit]:
        run = item.get("run") or {}
        game_id, game_name = fmt._id_and_name(item.get("game"))
        cat_id, cat_name = fmt._id_and_name(item.get("category"))
        name_map = fmt._player_name_map(item.get("players", {}))
        row = fmt.run_entry(run, place=item.get("place"), name_map=name_map)
        row["game"] = game_name or game_id
        row["category"] = cat_name or cat_id
        entries.append(row)
    return {
        "user": user,
        "returned": len(entries),
        "total_available": len(pbs),
        "personal_bests": entries,
    }


@mcp.tool(annotations=_read_anno("Get run"))
async def get_run(
    run_id: Annotated[str, Field(description="The run's id.")],
) -> dict:
    """Get the details of a single run: players, time, date, video and comment."""
    run = await _get_client().get_run(run_id, embed="players")
    name_map = fmt._player_name_map(run.get("players", {}))
    return fmt.run_entry(run, name_map=name_map)


@mcp.tool(annotations=_read_anno("List platforms"))
async def list_platforms() -> list[dict]:
    """List speedrun.com platforms (consoles/systems) with their ids and names.

    Use a returned ``id`` as the ``platform`` filter for ``get_leaderboard``
    (the leaderboard API requires the platform *id*, not its name).
    """
    platforms = await _get_client().get_platforms()
    return [{"id": p.get("id"), "name": p.get("name")} for p in platforms]


@mcp.tool(annotations=_read_anno("List regions"))
async def list_regions() -> list[dict]:
    """List speedrun.com regions (e.g. USA/NTSC, EUR/PAL) with their ids and names.

    Use a returned ``id`` as the ``region`` filter for ``get_leaderboard``
    (the leaderboard API requires the region *id*, not its name).
    """
    regions = await _get_client().get_regions()
    return [{"id": r.get("id"), "name": r.get("name")} for r in regions]


@mcp.tool(annotations=_read_anno("Search series"))
async def search_series(
    name: Annotated[str, Field(description="Series title or partial title to search for.")],
    limit: Annotated[int, Field(ge=1, le=50, description="Max series to return.")] = 10,
) -> list[dict]:
    """Fuzzy-search game series (e.g. 'Mario', 'Zelda') by name.

    A series groups related games; pass a returned id to ``get_series`` to list
    the games it contains.
    """
    series = await _get_client().search_series(name, maximum=limit)
    return [fmt.series_summary(s) for s in series]


@mcp.tool(annotations=_read_anno("Get series"))
async def get_series(
    series: Annotated[str, Field(description="Series id or abbreviation.")],
    include_games: Annotated[bool, Field(description="Also list the games in the series.")] = True,
    game_limit: Annotated[int, Field(ge=1, le=200, description="Max games to list.")] = 50,
) -> dict:
    """Get a series' details and (by default) the games it contains."""
    info = await _get_client().get_series(series)
    out = fmt.series_summary(info)
    if include_games:
        games = await _get_client().get_series_games(series, maximum=game_limit)
        out["games"] = [fmt.game_summary(g) for g in games]
    return out


@mcp.tool(annotations=_read_anno("List runs"))
async def list_runs(
    user: Annotated[
        str | None, Field(description="Filter to a player's runs (user id or username).")
    ] = None,
    game: Annotated[
        str | None, Field(description="Filter to a game (id or abbreviation).")
    ] = None,
    category: Annotated[str | None, Field(description="Filter to a category id.")] = None,
    status: Annotated[
        str | None, Field(description="Filter by status: 'new', 'verified', or 'rejected'.")
    ] = None,
    examiner: Annotated[
        str | None, Field(description="Filter to runs examined by this user id.")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=200, description="Max runs to return.")] = 20,
) -> list[dict]:
    """List runs with filters — e.g. a player's recent submissions, or a game's
    verified/rejected runs. Newest first; combine filters to narrow down.

    For just one game's moderation queue, ``list_unverified_runs`` is simpler.
    """
    runs = await _get_client().get_runs(
        user=user,
        game=game,
        category=category,
        status=status,
        examiner=examiner,
        orderby="submitted",
        direction="desc",
        maximum=limit,
        embed="players",
    )
    return [fmt.submission_result(r) for r in runs]


@mcp.tool(annotations=_read_anno("Get game records"))
async def get_game_records(
    game: Annotated[str, Field(description="Game id or abbreviation.")],
    top: Annotated[
        int, Field(ge=1, le=10, description="Places per category (1 = world records).")
    ] = 1,
    include_levels: Annotated[
        bool, Field(description="Include individual-level boards as well as full-game.")
    ] = False,
) -> dict:
    """Get a game's records across all its categories in one call.

    With ``top=1`` (default) this is every category's world record at once —
    handy for "show me all the records for <game>".
    """
    boards = await _get_client().get_game_records(
        game,
        top=top,
        scope="all" if include_levels else "full-game",
        embed="game,category,players,variables,level",
    )
    return {
        "game": game,
        "returned_boards": len(boards),
        "records": [fmt.leaderboard_view(b) for b in boards],
    }


# -- authenticated: identity (always on; need SPEEDRUN_API_KEY) ----------------


@_auth_tool(annotations=_read_anno("Who am I"))
async def whoami() -> dict:
    """Return the speedrun.com profile that owns the configured API key.

    Requires the ``SPEEDRUN_API_KEY`` environment variable. Handy to confirm
    which account a key belongs to before submitting or moderating runs.
    """
    profile = await _require_auth().get_profile()
    return fmt.profile_summary(profile)


@_auth_tool(annotations=_read_anno("List notifications"))
async def list_notifications(
    limit: Annotated[int, Field(ge=1, le=100, description="Max notifications to return.")] = 20,
    unread_only: Annotated[bool, Field(description="Only return unread notifications.")] = False,
) -> list[dict]:
    """List the authenticated user's notifications, newest first.

    Requires ``SPEEDRUN_API_KEY``.
    """
    notifs = await _require_auth().get_notifications()
    if unread_only:
        notifs = [n for n in notifs if n.get("status") == "unread"]
    return [fmt.notification_view(n) for n in notifs[:limit]]


@mcp.tool(annotations=_read_anno("List unverified runs"))
async def list_unverified_runs(
    game: Annotated[str, Field(description="Game id or abbreviation.")],
    limit: Annotated[int, Field(ge=1, le=200, description="Max runs to return.")] = 20,
) -> list[dict]:
    """List a game's runs awaiting verification — the moderation queue.

    A public read (no API key needed). Pair with ``verify_run`` / ``reject_run``
    (which do require a moderator key) to clear the queue.
    """
    runs = await _get_client().get_runs(
        status="new",
        game=game,
        orderby="submitted",
        direction="desc",
        maximum=limit,
        embed="players",
    )
    return [fmt.submission_result(r) for r in runs]


# -- authenticated: run submission & moderation -------------------------------
# Listed whenever a key is set, but each refuses to run (with a clear message)
# unless SPEEDRUN_ENABLE_WRITES is set — so read-only is the default and the way
# to enable writes is discoverable.


@_auth_tool(annotations=_write_anno("Submit a run"))
async def submit_run(
    category: Annotated[str, Field(description="Category id (from list_categories).")],
    platform: Annotated[str, Field(description="Platform id (from list_platforms).")],
    realtime: Annotated[
        float | None, Field(description="Real-time (RTA) in seconds.")
    ] = None,
    ingame: Annotated[float | None, Field(description="In-game time (IGT) in seconds.")] = None,
    realtime_noloads: Annotated[
        float | None, Field(description="Real-time without loads, in seconds.")
    ] = None,
    level: Annotated[
        str | None, Field(description="Level id, for an individual-level run.")
    ] = None,
    date: Annotated[str | None, Field(description="Run date as YYYY-MM-DD.")] = None,
    region: Annotated[str | None, Field(description="Region id (from list_regions).")] = None,
    video: Annotated[str | None, Field(description="Video proof URL.")] = None,
    comment: Annotated[str | None, Field(description="Run comment / description.")] = None,
    emulated: Annotated[bool, Field(description="Whether the run used an emulator.")] = False,
    variables: Annotated[
        dict[str, str] | None,
        Field(description="Subcategory choices as {variable_id: value_id} (from list_variables)."),
    ] = None,
) -> dict:
    """Submit a run to a leaderboard under your account (enters the mod queue).

    Needs write mode (set SPEEDRUN_ENABLE_WRITES=1). Provide at least one timing
    (``realtime`` / ``ingame`` / ``realtime_noloads``) in seconds. Resolve
    ``category`` / ``platform`` / ``variables`` ids with list_categories /
    list_platforms / list_variables first.
    """
    _require_writes()
    times: dict[str, float] = {}
    if realtime is not None:
        times["realtime"] = realtime
    if ingame is not None:
        times["ingame"] = ingame
    if realtime_noloads is not None:
        times["realtime_noloads"] = realtime_noloads
    if not times:
        raise ValueError(
            "Provide at least one time: realtime, ingame, or realtime_noloads (seconds)."
        )
    # The API keys variables by id with {"type", "value"}; subcategory selections
    # are pre-defined value ids.
    var_payload = (
        {vid: {"type": "pre-defined", "value": val} for vid, val in variables.items()}
        if variables
        else None
    )
    run = await _require_auth().submit_run(
        category=category,
        platform=platform,
        times=times,
        level=level,
        date=date,
        region=region,
        video=video,
        comment=comment,
        emulated=emulated,
        variables=var_payload,
    )
    return fmt.submission_result(run)


@_auth_tool(annotations=_write_anno("Verify a run", idempotent=True))
async def verify_run(
    run_id: Annotated[str, Field(description="The run id to verify.")],
) -> dict:
    """Mark a run as verified. Moderator only; needs write mode (SPEEDRUN_ENABLE_WRITES=1)."""
    _require_writes()
    run = await _require_auth().set_run_status(run_id, "verified")
    return fmt.submission_result(run)


@_auth_tool(annotations=_write_anno("Reject a run", destructive=True))
async def reject_run(
    run_id: Annotated[str, Field(description="The run id to reject.")],
    reason: Annotated[str, Field(description="Why the run is rejected (required).")],
) -> dict:
    """Reject a run with a reason. Moderator only; needs write mode (SPEEDRUN_ENABLE_WRITES=1)."""
    _require_writes()
    run = await _require_auth().set_run_status(run_id, "rejected", reason=reason)
    return fmt.submission_result(run)


@_auth_tool(annotations=_write_anno("Set run players", destructive=True))
async def set_run_players(
    run_id: Annotated[str, Field(description="The run id whose players to set.")],
    user_ids: Annotated[
        list[str] | None, Field(description="Registered player user ids.")
    ] = None,
    guests: Annotated[
        list[str] | None, Field(description="Guest player names (no account).")
    ] = None,
) -> dict:
    """Replace a run's player list. Moderator only; needs write mode (SPEEDRUN_ENABLE_WRITES=1).

    The new list *replaces* the existing one entirely — pass every player, not
    just additions.
    """
    _require_writes()
    players: list[dict[str, str]] = [{"rel": "user", "id": u} for u in (user_ids or [])]
    players += [{"rel": "guest", "name": g} for g in (guests or [])]
    if not players:
        raise ValueError("Provide at least one user_id or guest.")
    run = await _require_auth().set_run_players(run_id, players)
    return fmt.submission_result(run)


@_auth_tool(annotations=_write_anno("Delete a run", destructive=True))
async def delete_run(
    run_id: Annotated[str, Field(description="The run id to delete.")],
) -> dict:
    """Delete a run — your own, or any for global mods. Needs write mode (SPEEDRUN_ENABLE_WRITES=1). Irreversible."""
    _require_writes()
    run = await _require_auth().delete_run(run_id)
    return fmt.submission_result(run)


def main() -> None:
    """Console-script entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
