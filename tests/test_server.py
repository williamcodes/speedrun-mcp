"""Offline tests for server wiring (no network)."""

import json

import httpx
import pytest
from mcp.types import CallToolRequest, CallToolRequestParams

from speedrun_mcp import server as s
from speedrun_mcp.client import NotFoundError, SpeedrunClient, SpeedrunError


async def test_lifespan_closes_shared_client():
    """L8: the FastMCP lifespan must close the lazily-created HTTP client on shutdown."""
    client = s._get_client()  # creates the shared client (no socket until a request)
    assert s._client is client

    async with s._lifespan(s.mcp):
        pass  # server "runs" here; shutdown happens on context exit

    assert s._client is None  # closed and cleared
    assert client._http.is_closed


def test_truthy_env_parsing():
    assert s._truthy("1")
    assert s._truthy("true")
    assert s._truthy("YES")
    assert s._truthy("on")
    assert not s._truthy(None)
    assert not s._truthy("")
    assert not s._truthy("0")
    assert not s._truthy("false")


def test_annotations_mark_read_vs_write():
    read = s._read_anno("X")
    assert read.readOnlyHint is True

    write = s._write_anno("Y", destructive=True)
    assert write.readOnlyHint is False
    assert write.destructiveHint is True


async def test_tool_exposure_tracks_api_key():
    names = {t.name for t in await s.mcp.list_tools()}

    # public reads are always exposed (no key needed)
    assert {
        "search_games",
        "get_leaderboard",
        "list_unverified_runs",
        "search_series",
        "get_series",
        "list_runs",
        "get_game_records",
    } <= names

    # every authenticated tool — identity reads AND the write tools — is exposed
    # only when a key is configured (writes are still listed so they're
    # discoverable; the flag controls whether they *run*, not whether they show).
    # The functions are always defined on the module regardless.
    authed = (
        "whoami",
        "list_notifications",
        "submit_run",
        "verify_run",
        "reject_run",
        "set_run_players",
        "delete_run",
    )
    for name in authed:
        assert callable(getattr(s, name))
        assert (name in names) is s.AUTH_ENABLED


@pytest.mark.parametrize(
    "call",
    [
        lambda: s.submit_run(category="x", platform="y", realtime=1.0),
        lambda: s.verify_run(run_id="r"),
        lambda: s.reject_run(run_id="r", reason="bad"),
        lambda: s.set_run_players(run_id="r", user_ids=["u1"]),
        lambda: s.delete_run(run_id="r"),
    ],
    ids=["submit_run", "verify_run", "reject_run", "set_run_players", "delete_run"],
)
async def test_every_write_tool_blocked_when_read_only(call):
    # Each of the five write tools (incl. the irreversible delete_run) must refuse
    # in read-only mode with an actionable error naming SPEEDRUN_ENABLE_WRITES —
    # before any key or network access.
    if s.WRITES_ENABLED:
        pytest.skip("writes are enabled in this environment")
    with pytest.raises(RuntimeError) as excinfo:
        await call()
    assert "SPEEDRUN_ENABLE_WRITES" in str(excinfo.value)


async def test_whoami_requires_api_key(monkeypatch):
    monkeypatch.delenv("SPEEDRUN_API_KEY", raising=False)
    # force a fresh, unauthenticated singleton
    if s._client is not None:
        await s._client.close()
    s._client = None

    with pytest.raises(RuntimeError) as excinfo:
        await s.whoami()
    assert "SPEEDRUN_API_KEY" in str(excinfo.value)

    # clean up the client this test created
    if s._client is not None:
        await s._client.close()
        s._client = None


@pytest.fixture
def mock_api(monkeypatch):
    """Install a read-only MockTransport; use the returned client as a context manager."""

    def install(handler, *, api_key=None):
        def read_only(request):
            assert request.method == "GET"
            return handler(request)

        client = SpeedrunClient(api_key=api_key, transport=httpx.MockTransport(read_only))
        monkeypatch.setattr(s, "_client", client)
        return client

    return install


@pytest.mark.parametrize("tool", ["list_runs", "list_unverified_runs"])
async def test_run_tools_resolve_game_abbreviation(mock_api, tool):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/api/v1/games/sm64":
            return httpx.Response(200, json={"data": {"id": "o1y9wo6q"}})
        assert request.url.path == "/api/v1/runs"
        assert request.url.params["game"] == "o1y9wo6q"
        assert request.url.params["status"] == "new"
        return httpx.Response(200, json={"data": [{"id": "r1", "game": "o1y9wo6q"}]})

    async with mock_api(handler):
        if tool == "list_unverified_runs":
            rows = await s.list_unverified_runs("sm64")
        else:
            rows = await s.list_runs(game="sm64", status="new")
    assert rows["results"][0]["game"] == "o1y9wo6q"
    assert paths == ["/api/v1/games/sm64", "/api/v1/runs"]


async def test_list_runs_resolves_user_and_examiner_together(mock_api):
    def handler(request):
        if request.url.path == "/api/v1/users/Suigi":
            return httpx.Response(200, json={"data": {"id": "jn32931x"}})
        if request.url.path == "/api/v1/users/Moderator":
            return httpx.Response(200, json={"data": {"id": "mod12345"}})
        assert request.url.path == "/api/v1/runs"
        assert request.url.params["user"] == "jn32931x"
        assert request.url.params["examiner"] == "mod12345"
        return httpx.Response(200, json={"data": []})

    async with mock_api(handler):
        assert (await s.list_runs(user="Suigi", examiner="Moderator"))["results"] == []


async def test_personal_bests_preserves_not_found_error(mock_api):
    # Live probes of nonexistent users returned 404 rather than an empty list.
    def handler(request):
        assert request.url.path == "/api/v1/users/missing1/personal-bests"
        return httpx.Response(404, json={"message": "User missing1 could not be found."})

    async with mock_api(handler):
        with pytest.raises(NotFoundError, match="User missing1"):
            await s.get_user_personal_bests("missing1")


async def test_leaderboard_tool_rejects_invalid_date(mock_api):
    def handler(_request):
        pytest.fail("Invalid dates must fail before HTTP")

    async with mock_api(handler):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            await s.get_leaderboard("sm64", "wkpoo02r", date="yesterday")


@pytest.mark.parametrize("tool", ["get_leaderboard", "get_world_record"])
@pytest.mark.parametrize("echoed", [{}, {"valid": "yes"}, {"valid": "yes", "zzzz": "other"}])
async def test_leaderboard_tools_refuse_ignored_or_changed_variable_filters(mock_api, tool, echoed):
    def handler(request):
        assert request.url.params["var-zzzz"] == "yyyy"
        assert request.url.params["var-valid"] == "yes"
        return httpx.Response(200, json={"data": {"values": echoed, "runs": []}})

    async with mock_api(handler):
        with pytest.raises(ValueError, match=r"variable filters: .*zzzz"):
            await getattr(s, tool)("sm64", "wkpoo02r", variables={"zzzz": "yyyy", "valid": "yes"})


@pytest.mark.parametrize("tool", ["get_leaderboard", "get_world_record"])
async def test_leaderboard_tools_keep_api_error_for_invalid_known_value(mock_api, tool):
    def handler(_request):
        return httpx.Response(400, json={"message": "Invalid value; allowed values: yes, no"})

    async with mock_api(handler):
        with pytest.raises(SpeedrunError, match="allowed values: yes, no"):
            await getattr(s, tool)("sm64", "wkpoo02r", variables={"valid": "invalid"})


async def test_get_run_preserves_context_and_resolves_variable_labels(mock_api):
    run = {
        "id": "r1",
        "game": {"data": {"id": "g1", "names": {"international": "Example Game"}}},
        "category": {"data": {"id": "c1", "name": "Any%"}},
        "level": "l1",
        "status": {"status": "rejected", "examiner": "u2", "reason": "Spliced footage"},
        "system": {"platform": "p1", "emulated": False, "region": "region1"},
        "submitted": "2026-09-16T01:02:03Z",
        "players": {"data": [{"id": "u1", "names": {"international": "Alice"}}]},
        "times": {"primary_t": 50.5, "realtime_t": 53, "ingame_t": 50.5},
        "date": "2026-09-15",
        "videos": {"links": [{"uri": "https://example.com/video"}]},
        "comment": "A run",
        "values": {"v1": "easy", "v2": "english", "unknown": "raw", "custom": "My route"},
    }
    variables = [
        {
            "id": "v1",
            "name": "Difficulty",
            "is-subcategory": True,
            "values": {"values": {"easy": {"label": "Easy"}}},
        },
        {
            "id": "v2",
            "name": "Language",
            "is-subcategory": False,
            "values": {"values": {"english": {"label": "English"}}},
        },
        {"id": "custom", "name": "Route", "user-defined": True, "is-subcategory": False},
    ]

    def handler(request):
        if request.url.path == "/api/v1/runs/r1":
            assert request.url.params["embed"] == "game,category,players"
            return httpx.Response(200, json={"data": run})
        assert request.url.path == "/api/v1/games/g1/variables"
        return httpx.Response(200, json={"data": variables})

    async with mock_api(handler):
        out = await s.get_run("r1")
    assert out["game_id"] == "g1"
    assert out["game_name"] == "Example Game"
    assert out["category_id"] == "c1"
    assert out["category_name"] == "Any%"
    assert out["level"] == "l1"
    for field in ("status", "system", "submitted", "times", "values"):
        assert out[field] == run[field]
    assert out["players"] == ["Alice"]
    assert out["time_seconds"] == 50.5
    assert out["video"] == "https://example.com/video"
    assert out["comment"] == "A run"
    assert out["variables"]["v1"] == {
        "name": "Difficulty",
        "value": "easy",
        "label": "Easy",
        "is_subcategory": True,
    }
    assert out["variables"]["v2"]["is_subcategory"] is False
    assert out["variables"]["unknown"]["value"] == "raw"
    assert out["variables"]["unknown"]["label"] is None
    assert out["variables"]["custom"]["value"] == "My route"


async def test_get_run_with_no_variable_values_needs_no_extra_lookup(mock_api):
    def handler(request):
        assert request.url.path == "/api/v1/runs/r1"
        return httpx.Response(
            200,
            json={
                "data": {"id": "r1", "game": "g1", "category": "c1", "level": None, "values": {}}
            },
        )

    async with mock_api(handler):
        out = await s.get_run("r1")
    assert out["game_id"] == "g1"
    assert out["category_id"] == "c1"
    assert out["level"] is None
    assert out["values"] == out["variables"] == {}


@pytest.mark.parametrize(
    "tool",
    ["get_run", "get_game", "get_series", "list_categories", "list_variables", "get_game_records"],
)
async def test_resource_tools_reject_blank_ids(mock_api, tool):
    def handler(_request):
        pytest.fail("Blank ids must fail before HTTP")

    kwargs = {"include_games": False} if tool == "get_series" else {}
    async with mock_api(handler):
        with pytest.raises(ValueError, match="must be a non-empty value"):
            await getattr(s, tool)("", **kwargs)


@pytest.mark.parametrize("tool", ["search_games", "search_series"])
@pytest.mark.parametrize("query", ["", "マリオ"])
async def test_search_tools_do_not_return_unfiltered_collections(mock_api, tool, query):
    def handler(_request):
        pytest.fail("Ignored queries must fail before HTTP")

    async with mock_api(handler):
        with pytest.raises(ValueError, match="name must"):
            await getattr(s, tool)(query)


@pytest.mark.parametrize(
    ("tool", "args", "path"),
    [
        ("search_games", ["Mario"], "/games"),
        ("search_users", ["Alice"], "/users"),
        ("search_series", ["Mario"], "/series"),
        ("list_runs", [], "/runs"),
        ("list_unverified_runs", ["sm64"], "/runs"),
    ],
)
async def test_capped_tools_preserve_continuation(mock_api, tool, args, path):
    def handler(request):
        if request.url.path == "/api/v1/games/sm64":
            return httpx.Response(200, json={"data": {"id": "g1"}})
        assert request.url.path == f"/api/v1{path}"
        assert request.url.params["offset"] == "7"
        return httpx.Response(
            200,
            json={
                "data": [{"id": "i1"}],
                "pagination": {
                    "offset": 7,
                    "max": 1,
                    "links": [
                        {"rel": "next", "uri": f"https://www.speedrun.com/api/v1{path}?offset=8"}
                    ],
                },
            },
        )

    async with mock_api(handler):
        out = await getattr(s, tool)(*args, limit=1, offset=7)
    assert out["returned"] == len(out["results"]) == 1
    assert out["offset"] == 7
    assert out["limit"] == 1
    assert out["has_more"] is True
    assert out["next_offset"] == 8


async def test_series_games_have_their_own_page_envelope(mock_api):
    def handler(request):
        if request.url.path == "/api/v1/series/s1":
            return httpx.Response(200, json={"data": {"id": "s1"}})
        assert request.url.params["offset"] == "5"
        return httpx.Response(
            200, json={"data": [{"id": "g1"}], "pagination": {"offset": 5, "max": 1, "links": []}}
        )

    async with mock_api(handler):
        out = await s.get_series("s1", game_limit=1, game_offset=5)
    assert out["games"]["results"][0]["id"] == "g1"
    assert out["games"]["offset"] == 5
    assert out["games"]["has_more"] is False


async def test_empty_search_still_produces_model_readable_scope(mock_api):
    def handler(_request):
        return httpx.Response(200, json={"data": [], "pagination": {"links": []}})

    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name="search_games", arguments={"name": "Mario"}),
    )
    async with mock_api(handler):
        result = (await s.mcp._mcp_server.request_handlers[CallToolRequest](request)).root
    assert not result.isError
    assert result.content
    payload = json.loads(result.content[0].text)
    assert payload["returned"] == 0
    assert payload["results"] == []
    assert payload["has_more"] is False


async def test_unread_notifications_continue_past_first_hundred_read_records(mock_api):
    seen = []

    def handler(request):
        offset = int(request.url.params["offset"])
        seen.append(offset)
        data = (
            [{"id": str(i), "status": "read"} for i in range(100)]
            if offset == 0
            else [{"id": "unread1", "status": "unread"}]
        )
        links = (
            [{"rel": "next", "uri": "https://www.speedrun.com/api/v1/notifications?offset=100"}]
            if offset == 0
            else []
        )
        return httpx.Response(
            200, json={"data": data, "pagination": {"offset": offset, "links": links}}
        )

    async with mock_api(handler, api_key="test-key"):
        out = await s.list_notifications(unread_only=True)
    assert seen == [0, 100]
    assert out["results"][0]["id"] == "unread1"
    assert out["scanned"] == 101
    assert out["has_more"] is False


async def test_notification_scan_cap_does_not_imply_no_unread_notifications(mock_api):
    def handler(_request):
        return httpx.Response(
            200,
            json={
                "data": [{"id": str(i), "status": "read"} for i in range(100)],
                "pagination": {
                    "offset": 0,
                    "links": [
                        {
                            "rel": "next",
                            "uri": "https://www.speedrun.com/api/v1/notifications?offset=100",
                        }
                    ],
                },
            },
        )

    async with mock_api(handler, api_key="test-key"):
        out = await s.list_notifications(unread_only=True, scan_limit=100)
    assert out["returned"] == 0
    assert out["scanned"] == out["scan_limit"] == 100
    assert out["has_more"] is True
    assert out["next_offset"] == 100


async def test_notification_continuation_keeps_matches_omitted_by_limit(mock_api):
    def handler(request):
        offset = int(request.url.params["offset"])
        data = [
            {"id": "n0", "status": "read"},
            {"id": "n1", "status": "unread"},
            {"id": "n2", "status": "unread"},
        ]
        return httpx.Response(
            200, json={"data": data[offset:], "pagination": {"offset": offset, "links": []}}
        )

    async with mock_api(handler, api_key="test-key"):
        first = await s.list_notifications(unread_only=True, limit=1)
        second = await s.list_notifications(unread_only=True, limit=1, offset=first["next_offset"])
    assert first["results"][0]["id"] == "n1"
    assert first["has_more"] is True
    assert first["next_offset"] == 2
    assert second["results"][0]["id"] == "n2"
    assert second["has_more"] is False


async def test_personal_bests_keep_distinct_level_and_variable_identities(mock_api):
    def handler(_request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "place": 1,
                        "game": {"data": {"id": "g1", "names": {"international": "Game"}}},
                        "category": {"data": {"id": "c1", "name": "Any%"}},
                        "run": {
                            "id": level,
                            "game": "g1",
                            "category": "c1",
                            "level": level,
                            "values": {"v1": choice},
                            "times": {"primary_t": seconds},
                        },
                    }
                    for level, choice, seconds in (("l1", "easy", 50), ("l2", "hard", 60))
                ]
            },
        )

    async with mock_api(handler):
        rows = (await s.get_user_personal_bests("Alice"))["personal_bests"]
    assert [r["level"] for r in rows] == ["l1", "l2"]
    assert [r["values"] for r in rows] == [{"v1": "easy"}, {"v1": "hard"}]
    assert all(r["game_id"] == "g1" and r["category_id"] == "c1" for r in rows)
    assert all(r["timing"] == "primary" and r["time_source"] == "times.primary_t" for r in rows)


async def test_game_discovery_preserves_submission_constraints(mock_api):
    ruleset = {
        "run-times": ["ingame"],
        "default-time": "ingame",
        "require-video": True,
        "emulators-allowed": False,
    }

    def handler(_request):
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "g1",
                    "ruleset": ruleset,
                    "platforms": ["p1"],
                    "regions": ["r1"],
                    "categories": {
                        "data": [{"id": "c1", "players": {"type": "exactly", "value": 2}}]
                    },
                }
            },
        )

    async with mock_api(handler):
        out = await s.get_game("g1")
    assert out["ruleset"] == ruleset
    assert out["platforms"] == ["p1"]
    assert out["regions"] == ["r1"]
    assert out["categories"][0]["players"] == {"type": "exactly", "value": 2}


async def test_leaderboard_tool_echoes_requested_historical_and_system_filters(mock_api):
    def handler(request):
        assert request.url.params["date"] == "2020-01-01"
        return httpx.Response(
            200,
            json={
                "data": {
                    "values": {},
                    "runs": [],
                    "platform": "p1",
                    "region": "r1",
                    "emulators": False,
                }
            },
        )

    async with mock_api(handler):
        out = await s.get_leaderboard(
            "g1", "c1", date="2020-01-01", platform="p1", region="r1", emulators=False
        )
    assert out["requested_filters"]["date"] == "2020-01-01"
    assert out["applied_filters"]["emulators"] is False


async def test_record_tool_returns_boards_from_all_pages(mock_api):
    def handler(request):
        offset = int(request.url.params["offset"])
        board = {"game": "g1", "category": f"c{offset}", "runs": []}
        links = (
            [{"rel": "next", "uri": "https://www.speedrun.com/api/v1/games/g1/records?offset=1"}]
            if offset == 0
            else []
        )
        return httpx.Response(
            200, json={"data": [board], "pagination": {"offset": offset, "links": links}}
        )

    async with mock_api(handler):
        out = await s.get_game_records("g1", include_levels=True)
    assert out["returned_boards"] == 2
    assert [b["category_id"] for b in out["records"]] == ["c0", "c1"]
