"""Offline tests for server wiring (no network)."""

import pytest

from speedrun_mcp import server as s


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
        "search_games", "get_leaderboard", "list_unverified_runs",
        "search_series", "get_series", "list_runs", "get_game_records",
    } <= names

    # every authenticated tool — identity reads AND the write tools — is exposed
    # only when a key is configured (writes are still listed so they're
    # discoverable; the flag controls whether they *run*, not whether they show).
    # The functions are always defined on the module regardless.
    authed = (
        "whoami", "list_notifications",
        "submit_run", "verify_run", "reject_run", "set_run_players", "delete_run",
    )
    for name in authed:
        assert callable(getattr(s, name))
        assert (name in names) is s.AUTH_ENABLED


async def test_write_tools_blocked_with_clear_message_when_read_only():
    # A write call in read-only mode must fail with an actionable error naming
    # SPEEDRUN_ENABLE_WRITES — and do so before needing a key or the network.
    if s.WRITES_ENABLED:
        pytest.skip("writes are enabled in this environment")
    with pytest.raises(RuntimeError) as excinfo:
        await s.submit_run(category="x", platform="y", realtime=1.0)
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
