"""Offline tests for server wiring (no network)."""

from speedrun_mcp import server as s


async def test_lifespan_closes_shared_client():
    """L8: the FastMCP lifespan must close the lazily-created HTTP client on shutdown."""
    client = s._get_client()  # creates the shared client (no socket until a request)
    assert s._client is client

    async with s._lifespan(s.mcp):
        pass  # server "runs" here; shutdown happens on context exit

    assert s._client is None  # closed and cleared
    assert client._http.is_closed
