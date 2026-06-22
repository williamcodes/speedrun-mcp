"""Integration tests against the live speedrun.com API.

Run with ``pytest`` (included by default) or skip with ``pytest -m "not network"``.
These are deliberately lenient about exact values (records change) and assert on
structure and well-known invariants instead.
"""

import os
import re

import pytest

from speedrun_mcp import server as s
from speedrun_mcp.client import SpeedrunError

pytestmark = pytest.mark.network

SM64 = "o1y9wo6q"  # Super Mario 64

# Authenticated live tests only run when a real key is present. They are still
# under the `network` marker, so `pytest -m "not network"` skips them too.
requires_api_key = pytest.mark.skipif(
    not os.environ.get("SPEEDRUN_API_KEY"),
    reason="set SPEEDRUN_API_KEY to run authenticated live tests",
)


@pytest.fixture(autouse=True)
async def _fresh_client():
    """Reset the shared client after each test.

    pytest-asyncio runs each test on its own event loop, but the module-level
    client lazily binds its connection pool to whatever loop is active when it's
    first used. Closing and clearing it between tests keeps each test on a client
    bound to its own loop. (In production the server runs on a single loop, so
    the lazy singleton is fine there.)
    """
    yield
    if s._client is not None:
        await s._client.close()
        s._client = None


async def test_search_games_finds_sm64():
    games = await s.search_games("super mario 64", limit=5)
    assert any(g["id"] == SM64 for g in games)


async def test_get_game_returns_categories():
    game = await s.get_game("sm64")
    assert game["id"] == SM64
    names = [c["name"] for c in game["categories"]]
    assert "16 Star" in names


async def test_world_record_is_place_one_with_a_time():
    cats = await s.list_categories("sm64")
    sixteen = next(c for c in cats if c["name"] == "16 Star")
    wr = await s.get_world_record("sm64", sixteen["id"])
    rec = wr["world_record"]
    assert rec["place"] == 1
    assert rec["players"]
    assert rec["time_seconds"] > 0


async def test_leaderboard_is_sorted_by_place():
    cats = await s.list_categories("sm64")
    sixteen = next(c for c in cats if c["name"] == "16 Star")
    lb = await s.get_leaderboard("sm64", sixteen["id"], top=5)
    places = [r["place"] for r in lb["runs"]]
    assert places == sorted(places)
    times = [r["time_seconds"] for r in lb["runs"]]
    assert times == sorted(times)


def _looks_like_user_id(value: str) -> bool:
    # speedrun.com user ids are 8-char base62-ish slugs (e.g. 'jn32931x').
    return bool(re.fullmatch(r"[a-z0-9]{8}", value))


async def test_personal_bests_resolve_player_names_not_ids():
    pbs = await s.get_user_personal_bests("Suigi", limit=10)
    assert pbs["total_available"] >= 1
    assert pbs["returned"] == len(pbs["personal_bests"])
    assert "count" not in pbs  # old misleading key is gone
    # at least one PB should resolve to the human name, not a raw id.
    names = [name for pb in pbs["personal_bests"] for name in pb.get("players", [])]
    assert names, "expected resolved player names on PBs"
    assert "Suigi" in names
    # and not the raw user-id form for Suigi's own runs
    assert not all(_looks_like_user_id(n) for n in names)


async def test_search_users_partial_match():
    users = await s.search_users("sui", limit=20)
    assert users, "partial search should return matches"
    # 'sui' is a substring (fuzzy 'name' query, not exact 'lookup')
    assert any("sui" in (u["name"] or "").lower() for u in users)


async def test_level_on_full_game_category_surfaces_api_message():
    game = await s.get_game("sm64", include_levels=True)
    real_level = game["levels"][0]["id"]  # a genuine level id (avoids a 404)
    cats = await s.list_categories("sm64")
    full_game = next(c for c in cats if c["type"] == "per-game")
    with pytest.raises(SpeedrunError) as excinfo:
        # passing a (real) level id for a full-game category -> 400 from the API
        await s.get_leaderboard("sm64", full_game["id"], level=real_level, top=1)
    # the API's explanatory message must be surfaced, not swallowed.
    assert "full-game" in str(excinfo.value).lower()


async def test_individual_level_leaderboard_with_level_id():
    game = await s.get_game("sm64", include_levels=True)
    assert game["levels"], "sm64 should expose individual levels"
    level_id = game["levels"][0]["id"]
    # find a per-level category
    cats = await s.list_categories("sm64")
    il_cat = next(c for c in cats if c["type"] == "per-level")
    lb = await s.get_leaderboard("sm64", il_cat["id"], level=level_id, top=5)
    assert lb["level"] == level_id
    places = [r["place"] for r in lb["runs"]]
    assert places == sorted(places)


async def test_world_record_returns_world_record_and_tied_list():
    cats = await s.list_categories("sm64")
    sixteen = next(c for c in cats if c["name"] == "16 Star")
    wr = await s.get_world_record("sm64", sixteen["id"])
    assert wr["world_record"]["place"] == 1
    # 'tied' always present; entries (if any) are also place 1.
    assert isinstance(wr["tied"], list)
    assert all(r["place"] == 1 for r in wr["tied"])


async def test_list_platforms_paginates_beyond_one_page():
    # /platforms exceeds the 200-item page cap, so a single request truncates.
    # The client must follow pagination links and return every platform.
    platforms = await s.list_platforms()
    assert len(platforms) > 200, "platform list should span multiple pages"
    names = {p["name"] for p in platforms}
    # these sort after the first 200 (V/W/X) — missing before pagination was added.
    assert {"Wii", "Xbox"}.issubset(names)


async def test_list_unverified_runs_is_public():
    # The moderation-queue read needs no API key; it should return run summaries
    # (or an empty list if the queue happens to be clear).
    runs = await s.list_unverified_runs("sm64", limit=5)
    assert isinstance(runs, list)
    for r in runs:
        assert "run_id" in r


async def test_search_series_finds_results():
    series = await s.search_series("Mario", limit=10)
    assert series, "expected at least one series matching 'Mario'"
    assert all("id" in x for x in series)


async def test_get_series_lists_its_games():
    matches = await s.search_series("Super Mario", limit=1)
    assert matches, "expected a 'Super Mario' series"
    detail = await s.get_series(matches[0]["id"])
    assert detail["id"] == matches[0]["id"]
    assert isinstance(detail.get("games"), list)


async def test_list_runs_filters_by_game_and_status():
    runs = await s.list_runs(game=SM64, status="verified", limit=5)
    assert runs, "sm64 should have verified runs"
    assert all(r.get("status") == "verified" for r in runs)


async def test_get_game_records_returns_world_records():
    recs = await s.get_game_records("sm64", top=1)
    assert recs["game"] == "sm64"
    assert recs["records"], "expected at least one category board"
    # with top=1, at least one board should carry a world-record run
    assert any(b.get("runs") for b in recs["records"])


@requires_api_key
async def test_whoami_returns_the_keys_profile():
    me = await s.whoami()
    assert me["id"]
    assert me["name"]


@requires_api_key
async def test_list_notifications_returns_list():
    notes = await s.list_notifications(limit=5)
    assert isinstance(notes, list)
