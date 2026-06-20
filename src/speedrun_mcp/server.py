"""MCP server exposing speedrun.com data as tools.

Typical flow for a model:
  1. ``search_games`` to turn a title into a game id.
  2. ``list_categories`` (and ``list_variables`` for subcategories) to pick a
     category/filters.
  3. ``get_leaderboard`` / ``get_world_record`` for the actual rankings.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from mcp.server.fastmcp import FastMCP
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
        _client = SpeedrunClient()
    return _client


@mcp.tool()
async def search_games(
    name: Annotated[str, Field(description="Game title or partial title to search for.")],
    limit: Annotated[int, Field(ge=1, le=50, description="Max games to return.")] = 10,
) -> list[dict]:
    """Fuzzy-search games by name. Returns ids, abbreviations and release years.

    Use the returned ``id`` (or ``abbreviation``) with the other tools.
    """
    games = await _get_client().search_games(name, maximum=limit)
    return [fmt.game_summary(g) for g in games]


@mcp.tool()
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


@mcp.tool()
async def list_categories(
    game: Annotated[str, Field(description="Game id or abbreviation.")],
) -> list[dict]:
    """List a game's categories (e.g. 'Any%', '120 Star'), with their ids and rules."""
    cats = await _get_client().get_categories(game)
    return [fmt.category_summary(c) for c in cats]


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
async def search_users(
    name: Annotated[str, Field(description="Username (or partial) to look up.")],
    limit: Annotated[int, Field(ge=1, le=50, description="Max users to return.")] = 10,
) -> list[dict]:
    """Search for speedrun.com users by name. Returns ids, countries and signup dates."""
    users = await _get_client().search_users(name, maximum=limit)
    return [fmt.user_summary(u) for u in users]


@mcp.tool()
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


@mcp.tool()
async def get_run(
    run_id: Annotated[str, Field(description="The run's id.")],
) -> dict:
    """Get the details of a single run: players, time, date, video and comment."""
    run = await _get_client().get_run(run_id, embed="players")
    name_map = fmt._player_name_map(run.get("players", {}))
    return fmt.run_entry(run, name_map=name_map)


@mcp.tool()
async def list_platforms() -> list[dict]:
    """List speedrun.com platforms (consoles/systems) with their ids and names.

    Use a returned ``id`` as the ``platform`` filter for ``get_leaderboard``
    (the leaderboard API requires the platform *id*, not its name).
    """
    platforms = await _get_client().get_platforms()
    return [{"id": p.get("id"), "name": p.get("name")} for p in platforms]


@mcp.tool()
async def list_regions() -> list[dict]:
    """List speedrun.com regions (e.g. USA/NTSC, EUR/PAL) with their ids and names.

    Use a returned ``id`` as the ``region`` filter for ``get_leaderboard``
    (the leaderboard API requires the region *id*, not its name).
    """
    regions = await _get_client().get_regions()
    return [{"id": r.get("id"), "name": r.get("name")} for r in regions]


def main() -> None:
    """Console-script entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
