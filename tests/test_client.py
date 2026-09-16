"""Offline tests for the authenticated client paths (no network).

These use httpx's MockTransport to assert on exactly what the client *sends*
(method, path, headers, JSON body) and how it maps error statuses — without
ever touching speedrun.com.
"""

import json

import httpx
import pytest

from speedrun_mcp.client import (
    AuthError,
    NotFoundError,
    RateLimitError,
    SpeedrunClient,
    SpeedrunError,
)


def _transport(handler):
    return httpx.MockTransport(handler)


async def test_api_key_header_and_flag():
    async with SpeedrunClient(api_key="abc123") as c:
        assert c.authenticated is True
        assert c._http.headers.get("x-api-key") == "abc123"
    async with SpeedrunClient() as c:
        assert c.authenticated is False
        assert "x-api-key" not in c._http.headers


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_raises_auth_error(status):
    def handler(_request):
        return httpx.Response(
            status,
            json={
                "status": status,
                "message": (
                    "This operations requires a user context, but no valid API "
                    "Key was submitted in your request."
                ),
            },
        )

    async with SpeedrunClient(transport=_transport(handler)) as c:
        with pytest.raises(AuthError) as excinfo:
            await c.get_profile()
    assert "user context" in str(excinfo.value)


@pytest.mark.parametrize(
    ("status", "error", "message"),
    [
        (401, AuthError, "speedrun.com rejected PUT /runs/r1/status"),
        (403, AuthError, "speedrun.com rejected PUT /runs/r1/status"),
        (404, NotFoundError, "Not found: /runs/r1/status"),
        (500, SpeedrunError, "speedrun.com returned HTTP 500 for /runs/r1/status"),
    ],
)
@pytest.mark.parametrize("content", [b"", b"<html>Unavailable</html>", b"[]"])
async def test_http_errors_without_api_details(status, error, message, content):
    async with SpeedrunClient(
        transport=_transport(lambda _r: httpx.Response(status, content=content))
    ) as c:
        with pytest.raises(error, match=message) as excinfo:
            await c.set_run_status("r1", "verified")

    assert type(excinfo.value) is error
    assert "speedrun.com says" not in str(excinfo.value)


async def test_validation_errors_surface_field_reasons():
    def handler(_request):
        return httpx.Response(
            400,
            json={
                "status": 400,
                "message": "The submitted run does not validate against the schema.",
                "errors": ["[category] is missing and it is required"],
            },
        )

    async with SpeedrunClient(api_key="k", transport=_transport(handler)) as c:
        with pytest.raises(SpeedrunError) as excinfo:
            # Valid-looking input so it passes local validation and reaches the
            # (mocked) API, whose schema rejection we want surfaced.
            await c.submit_run(category="cat", platform="plat", times={"realtime": 1.0})
    msg = str(excinfo.value)
    assert "does not validate" in msg
    assert "[category] is missing" in msg  # the per-field reason is surfaced


async def test_submit_run_wraps_body_and_sends_key():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["key"] = request.headers.get("x-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={"data": {"id": "newrun", "weblink": "https://w", "status": {"status": "new"}}},
        )

    async with SpeedrunClient(api_key="k", transport=_transport(handler)) as c:
        data = await c.submit_run(
            category="cat",
            platform="plat",
            times={"realtime": 12.34},
            variables={"v1": {"type": "pre-defined", "value": "val1"}},
        )

    assert seen["method"] == "POST"
    assert seen["path"].endswith("/runs")
    assert seen["key"] == "k"
    assert seen["body"] == {
        "run": {
            "category": "cat",
            "platform": "plat",
            "times": {"realtime": 12.34},
            "variables": {"v1": {"type": "pre-defined", "value": "val1"}},
        }
    }
    assert data["id"] == "newrun"


async def test_set_run_status_double_nests_reason():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"data": {"id": "r1", "status": {"status": "rejected", "reason": "x"}}}
        )

    async with SpeedrunClient(api_key="k", transport=_transport(handler)) as c:
        await c.set_run_status("r1", "rejected", reason="spliced footage")

    assert seen["method"] == "PUT"
    assert seen["path"].endswith("/runs/r1/status")
    assert seen["body"] == {"status": {"status": "rejected", "reason": "spliced footage"}}


async def test_verify_status_omits_reason():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"id": "r1", "status": {"status": "verified"}}})

    async with SpeedrunClient(api_key="k", transport=_transport(handler)) as c:
        await c.set_run_status("r1", "verified")

    assert seen["body"] == {"status": {"status": "verified"}}  # no reason key


async def test_get_game_records_path_and_params():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"data": [], "pagination": {"links": []}})

    async with SpeedrunClient(transport=_transport(handler)) as c:
        await c.get_game_records("sm64", top=1, scope="full-game", embed="game,category")

    assert seen["method"] == "GET"
    assert seen["path"].endswith("/games/sm64/records")
    assert seen["query"]["top"] == "1"
    assert seen["query"]["scope"] == "full-game"
    # None-valued params (miscellaneous) must be dropped, not sent as "None"
    assert "miscellaneous" not in seen["query"]


async def test_list_runs_passes_user_filter():
    seen = {}

    def handler(request):
        if request.url.path == "/api/v1/users/jn32931x":
            return httpx.Response(200, json={"data": {"id": "jn32931x"}})
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"data": []})

    async with SpeedrunClient(transport=_transport(handler)) as c:
        await c.get_runs(user="jn32931x", status="verified", maximum=5)

    assert seen["path"].endswith("/runs")
    assert seen["query"]["user"] == "jn32931x"
    assert seen["query"]["status"] == "verified"
    assert seen["query"]["max"] == "5"


async def test_delete_run_uses_delete_method():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": {"id": "r1", "status": {"status": "verified"}}})

    async with SpeedrunClient(api_key="k", transport=_transport(handler)) as c:
        data = await c.delete_run("r1")

    assert seen["method"] == "DELETE"
    assert seen["path"].endswith("/runs/r1")
    assert data["id"] == "r1"


async def test_delete_run_tolerates_empty_body():
    # A 204 / empty write response must come back as None, not crash.
    async with SpeedrunClient(
        api_key="k", transport=_transport(lambda _r: httpx.Response(204))
    ) as c:
        assert await c.delete_run("r1") is None


async def test_rate_limit_status_raises_rate_limit_error():
    async with SpeedrunClient(transport=_transport(lambda _r: httpx.Response(420))) as c:
        with pytest.raises(RateLimitError):
            await c.search_games("x")


async def test_not_found_status_raises_not_found_error():
    def handler(_request):
        return httpx.Response(404, json={"status": 404, "message": "Not found."})

    async with SpeedrunClient(transport=_transport(handler)) as c:
        with pytest.raises(NotFoundError) as excinfo:
            await c.get_game("bogus")
    assert "speedrun.com says" in str(excinfo.value)


async def test_set_run_players_body_shape():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"id": "r1", "status": {"status": "verified"}}})

    async with SpeedrunClient(api_key="k", transport=_transport(handler)) as c:
        await c.set_run_players(
            "r1", [{"rel": "user", "id": "u1"}, {"rel": "guest", "name": "Bob"}]
        )

    assert seen["method"] == "PUT"
    assert seen["path"].endswith("/runs/r1/players")
    assert seen["body"] == {
        "players": [{"rel": "user", "id": "u1"}, {"rel": "guest", "name": "Bob"}]
    }


async def test_submit_run_requires_a_time():
    async with SpeedrunClient(
        api_key="k", transport=_transport(lambda _r: httpx.Response(201))
    ) as c:
        with pytest.raises(ValueError, match="times needs at least one"):
            await c.submit_run(category="c", platform="p", times={})


async def test_reject_requires_a_reason():
    async with SpeedrunClient(
        api_key="k", transport=_transport(lambda _r: httpx.Response(200))
    ) as c:
        with pytest.raises(ValueError, match="Rejecting a run requires a non-empty reason"):
            await c.set_run_status("r1", "rejected")


async def test_submit_run_rejects_invalid_times():
    async with SpeedrunClient(
        api_key="k", transport=_transport(lambda _r: httpx.Response(201))
    ) as c:
        for bad in (-1.0, 0.0, float("inf"), float("nan")):
            with pytest.raises(ValueError, match="time 'realtime' must be a positive, finite"):
                await c.submit_run(category="c", platform="p", times={"realtime": bad})


async def test_submit_run_rejects_blank_ids_bad_date_and_video():
    async with SpeedrunClient(
        api_key="k", transport=_transport(lambda _r: httpx.Response(201))
    ) as c:
        with pytest.raises(ValueError, match="category must be a non-empty value"):
            await c.submit_run(category="   ", platform="p", times={"realtime": 1.0})
        with pytest.raises(ValueError, match="platform must be a non-empty value"):
            await c.submit_run(category="c", platform="", times={"realtime": 1.0})
        with pytest.raises(ValueError, match="date must be in YYYY-MM-DD form"):
            await c.submit_run(
                category="c", platform="p", times={"realtime": 1.0}, date="01/02/2023"
            )
        with pytest.raises(ValueError, match=r"video must be an http\(s\) URL"):
            await c.submit_run(
                category="c", platform="p", times={"realtime": 1.0}, video="not-a-url"
            )


async def test_reject_requires_a_nonblank_reason():
    async with SpeedrunClient(
        api_key="k", transport=_transport(lambda _r: httpx.Response(200))
    ) as c:
        with pytest.raises(ValueError, match="Rejecting a run requires a non-empty reason"):
            await c.set_run_status("r1", "rejected", reason="   ")


async def test_write_methods_reject_blank_run_id():
    async with SpeedrunClient(
        api_key="k", transport=_transport(lambda _r: httpx.Response(200))
    ) as c:
        with pytest.raises(ValueError, match="run_id must be a non-empty value"):
            await c.delete_run("")
        with pytest.raises(ValueError, match="run_id must be a non-empty value"):
            await c.set_run_players("  ", [{"rel": "user", "id": "u1"}])
        with pytest.raises(ValueError, match="run_id must be a non-empty value"):
            await c.set_run_status("", "verified")


async def test_get_paginated_walks_pages_and_clamps_to_200():
    # Page 0 is full (200) with a next link; page 1 is short (final). A max>200
    # request must be clamped to 200 so the short-page break can't truncate
    # after page 0.
    pages = {
        0: {
            "data": [{"id": f"a{i}"} for i in range(200)],
            "pagination": {"links": [{"rel": "next", "uri": "x"}]},
        },
        200: {"data": [{"id": "b0"}, {"id": "b1"}], "pagination": {"links": []}},
    }
    seen = []

    def handler(request):
        off = int(request.url.params.get("offset", 0))
        mx = int(request.url.params.get("max", 0))
        seen.append((off, mx))
        return httpx.Response(200, json=pages[off])

    async with SpeedrunClient(transport=_transport(handler)) as c:
        items = await c._get_paginated("/platforms", {"max": 500})

    assert [off for off, _ in seen] == [0, 200]  # walked both pages and stopped
    assert all(mx == 200 for _, mx in seen)  # clamped, never requested 500
    assert len(items) == 202


@pytest.mark.parametrize(
    ("field", "resource", "value", "resolved_id"),
    [
        ("game", "games", "sm64", "o1y9wo6q"),
        ("user", "users", "Suigi", "jn32931x"),
        ("examiner", "users", "Suigi", "jn32931x"),
        # Eight-character names and abbreviations must not be mistaken for ids.
        ("user", "users", "Zzzzzzzz", "xyrn97yj"),
        ("game", "games", "longabbr", "o1y9wo6q"),
        ("category", "categories", "wkpoo02r", "wkpoo02r"),
        ("level", "levels", "abc12345", "abc12345"),
    ],
)
async def test_runs_resolves_resource_filters(field, resource, value, resolved_id):
    seen = []

    def handler(request):
        assert request.method == "GET"
        seen.append(request.url.path)
        if request.url.path == f"/api/v1/{resource}/{value}":
            if value != resolved_id:
                return httpx.Response(
                    302, headers={"Location": f"/api/v1/{resource}/{resolved_id}"}
                )
            return httpx.Response(200, json={"data": {"id": resolved_id}})
        if request.url.path == f"/api/v1/{resource}/{resolved_id}":
            return httpx.Response(200, json={"data": {"id": resolved_id}})
        assert request.url.path == "/api/v1/runs"
        assert request.url.params[field] == resolved_id
        return httpx.Response(200, json={"data": [{"id": "r1"}]})

    async with SpeedrunClient(transport=_transport(handler)) as client:
        assert (await client.get_runs(**{field: value}))["data"] == [{"id": "r1"}]
    assert seen[0] == f"/api/v1/{resource}/{value}"
    assert seen[-1] == "/api/v1/runs"


@pytest.mark.parametrize("field", ["game", "user", "examiner", "category", "level"])
async def test_runs_invalid_resource_never_falls_back_to_unfiltered_runs(field):
    def handler(request):
        assert request.method == "GET"
        assert request.url.path != "/api/v1/runs"
        return httpx.Response(404, json={"message": "Resource does not exist"})

    async with SpeedrunClient(transport=_transport(handler)) as client:
        with pytest.raises(NotFoundError, match="Resource does not exist"):
            await client.get_runs(**{field: "missing1"})


@pytest.mark.parametrize("blank", ["", " \t\n"])
@pytest.mark.parametrize(
    ("method", "field"),
    [
        ("get_game", "game"),
        ("get_categories", "game"),
        ("get_levels", "game"),
        ("get_game_variables", "game"),
        ("get_category_variables", "category"),
        ("get_game_records", "game"),
        ("get_user", "user"),
        ("get_user_personal_bests", "user"),
        ("get_run", "run_id"),
        ("get_series", "series"),
        ("get_series_games", "series"),
        ("search_games", "name"),
        ("search_series", "name"),
        ("search_users", "name"),
    ],
)
async def test_read_methods_reject_blank_identifiers_before_http(method, field, blank):
    def handler(_request):
        pytest.fail("Blank input must not reach the API")

    async with SpeedrunClient(transport=_transport(handler)) as client:
        with pytest.raises(ValueError, match=f"{field} must be a non-empty value"):
            await getattr(client, method)(blank)


@pytest.mark.parametrize("field", ["game", "user", "examiner", "category", "level"])
async def test_runs_rejects_blank_filters_before_http(field):
    def handler(_request):
        pytest.fail("Blank filters must not reach the API")

    async with SpeedrunClient(transport=_transport(handler)) as client:
        with pytest.raises(ValueError, match=f"{field} must be a non-empty value"):
            await client.get_runs(**{field: " "})


@pytest.mark.parametrize("field", ["game", "category", "level"])
async def test_leaderboard_rejects_blank_identifiers_before_http(field):
    def handler(_request):
        pytest.fail("Blank identifiers must not reach the API")

    args = {"game": "sm64", "category": "wkpoo02r", field: ""}
    async with SpeedrunClient(transport=_transport(handler)) as client:
        with pytest.raises(ValueError, match=f"{field} must be a non-empty value"):
            await client.get_leaderboard(**args)


@pytest.mark.parametrize("date", ["yesterday", "", "2026-02-30", "20260916", "2026-W38-3"])
async def test_leaderboard_rejects_invalid_dates_before_http(date):
    def handler(_request):
        pytest.fail("Invalid dates must not reach the API")

    async with SpeedrunClient(transport=_transport(handler)) as client:
        with pytest.raises(ValueError, match="date must be in YYYY-MM-DD form"):
            await client.get_leaderboard("sm64", "wkpoo02r", date=date)


async def test_leaderboard_preserves_valid_date_and_filters():
    def handler(request):
        assert request.method == "GET"
        assert request.url.params["date"] == "2024-02-29"
        assert request.url.params["var-v1"] == "x1"
        return httpx.Response(200, json={"data": {"values": {"v1": "x1"}, "runs": []}})

    async with SpeedrunClient(transport=_transport(handler)) as client:
        board = await client.get_leaderboard(
            "sm64", "wkpoo02r", date="2024-02-29", variables={"v1": "x1"}
        )
    assert board["values"] == {"v1": "x1"}


@pytest.mark.parametrize("method", ["search_games", "search_series"])
@pytest.mark.parametrize("query", ["マリオ", "Марио", "马里奥", "!!!", "\uff11\uff12\uff13"])
async def test_title_search_rejects_queries_the_api_ignores(method, query):
    def handler(_request):
        pytest.fail("Unsupported searches must not reach the API")

    async with SpeedrunClient(transport=_transport(handler)) as client:
        with pytest.raises(ValueError, match="Latin letter or ASCII digit"):
            await getattr(client, method)(query)


@pytest.mark.parametrize("method", ["search_games", "search_series"])
@pytest.mark.parametrize("query", ["Mario", "Pokémon", "é", "Ø", "ß", "1942", "マリオ Mario"])
async def test_title_search_preserves_supported_queries(method, query):
    def handler(request):
        assert request.method == "GET"
        assert request.url.params["name"] == query
        return httpx.Response(200, json={"data": []})

    async with SpeedrunClient(transport=_transport(handler)) as client:
        assert (await getattr(client, method)(query))["data"] == []


async def test_records_follows_next_link_even_after_a_short_page():
    seen = []

    def handler(request):
        assert request.method == "GET"
        offset = int(request.url.params["offset"])
        seen.append(offset)
        data = [{"category": "c1"}] if offset == 0 else [{"category": "c2"}]
        links = (
            [{"rel": "next", "uri": "https://www.speedrun.com/api/v1/games/g1/records?offset=7"}]
            if offset == 0
            else []
        )
        return httpx.Response(
            200, json={"data": data, "pagination": {"offset": offset, "max": 200, "links": links}}
        )

    async with SpeedrunClient(transport=_transport(handler)) as client:
        rows = await client.get_game_records("g1")
    assert seen == [0, 7]
    assert [r["category"] for r in rows] == ["c1", "c2"]


async def test_complete_collection_refuses_to_claim_completeness_without_metadata():
    async with SpeedrunClient(
        transport=_transport(lambda _: httpx.Response(200, json={"data": []}))
    ) as client:
        with pytest.raises(SpeedrunError, match="completeness is unknown"):
            await client.get_game_records("g1")


async def test_page_reports_unknown_completeness_when_api_omits_pagination():
    async with SpeedrunClient(
        transport=_transport(lambda _: httpx.Response(200, json={"data": [{"id": "g1"}]}))
    ) as client:
        page = await client.search_games("Mario", maximum=1, offset=5)
    assert page["has_more"] is None
    assert page["next_offset"] == 6


async def test_page_rejects_nonadvancing_continuation():
    body = {
        "data": [],
        "pagination": {
            "offset": 1,
            "links": [{"rel": "next", "uri": "https://www.speedrun.com/api/v1/games?offset=1"}],
        },
    }
    async with SpeedrunClient(
        transport=_transport(lambda _: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(SpeedrunError, match="Pagination did not advance"):
            await client.search_games("Mario", offset=1)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
async def test_write_timeout_reports_uncertain_outcome_without_retrying(method):
    seen = []

    def handler(request):
        seen.append(request)
        raise httpx.ReadTimeout("Timed out waiting for response", request=request)

    async with SpeedrunClient(transport=_transport(handler)) as client:
        with pytest.raises(SpeedrunError, match="Write outcome unknown") as excinfo:
            await client._request("/runs/r1", method=method)
    assert "may have succeeded" in str(excinfo.value)
    assert "do not repeat the write automatically" in str(excinfo.value)
    assert len(seen) == 1


async def test_unparseable_write_success_preserves_status_and_location():
    response = httpx.Response(
        201, content=b"not JSON", headers={"Location": "https://www.speedrun.com/api/v1/runs/r1"}
    )
    async with SpeedrunClient(transport=_transport(lambda _: response)) as client:
        with pytest.raises(SpeedrunError, match="acknowledged POST /runs with HTTP 201") as excinfo:
            await client._request("/runs", method="POST")
    assert "may have succeeded" in str(excinfo.value)
    assert "do not repeat the write automatically" in str(excinfo.value)
    assert "Resource location: https://www.speedrun.com/api/v1/runs/r1" in str(excinfo.value)


async def test_write_server_error_warns_that_outcome_is_unknown():
    async with SpeedrunClient(transport=_transport(lambda _: httpx.Response(503))) as client:
        with pytest.raises(SpeedrunError, match="Write outcome unknown"):
            await client._request("/runs", method="POST")
