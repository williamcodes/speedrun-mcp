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


async def test_auth_failure_raises_auth_error():
    def handler(_request):
        return httpx.Response(
            403,
            json={
                "status": 403,
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
        return httpx.Response(200, json={"data": []})

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
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"data": []})

    async with SpeedrunClient(transport=_transport(handler)) as c:
        await c.get_runs(user="u123", status="verified", maximum=5)

    assert seen["path"].endswith("/runs")
    assert seen["query"]["user"] == "u123"
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
        with pytest.raises(ValueError):
            await c.submit_run(category="c", platform="p", times={})


async def test_reject_requires_a_reason():
    async with SpeedrunClient(
        api_key="k", transport=_transport(lambda _r: httpx.Response(200))
    ) as c:
        with pytest.raises(ValueError):
            await c.set_run_status("r1", "rejected")


async def test_submit_run_rejects_invalid_times():
    async with SpeedrunClient(
        api_key="k", transport=_transport(lambda _r: httpx.Response(201))
    ) as c:
        for bad in (-1.0, 0.0, float("inf"), float("nan")):
            with pytest.raises(ValueError):
                await c.submit_run(category="c", platform="p", times={"realtime": bad})


async def test_submit_run_rejects_blank_ids_bad_date_and_video():
    async with SpeedrunClient(
        api_key="k", transport=_transport(lambda _r: httpx.Response(201))
    ) as c:
        with pytest.raises(ValueError):
            await c.submit_run(category="   ", platform="p", times={"realtime": 1.0})
        with pytest.raises(ValueError):
            await c.submit_run(category="c", platform="", times={"realtime": 1.0})
        with pytest.raises(ValueError):
            await c.submit_run(
                category="c", platform="p", times={"realtime": 1.0}, date="01/02/2023"
            )
        with pytest.raises(ValueError):
            await c.submit_run(
                category="c", platform="p", times={"realtime": 1.0}, video="not-a-url"
            )


async def test_reject_requires_a_nonblank_reason():
    async with SpeedrunClient(
        api_key="k", transport=_transport(lambda _r: httpx.Response(200))
    ) as c:
        with pytest.raises(ValueError):
            await c.set_run_status("r1", "rejected", reason="   ")


async def test_write_methods_reject_blank_run_id():
    async with SpeedrunClient(
        api_key="k", transport=_transport(lambda _r: httpx.Response(200))
    ) as c:
        with pytest.raises(ValueError):
            await c.delete_run("")
        with pytest.raises(ValueError):
            await c.set_run_players("  ", [{"rel": "user", "id": "u1"}])
        with pytest.raises(ValueError):
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
