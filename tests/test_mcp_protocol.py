"""Protocol-level smoke tests: drive the real server over MCP stdio.

These exercise the FastMCP registration / schema / annotation / dispatch layer
that the direct-call unit tests bypass. They spawn ``python -m speedrun_mcp`` and
talk to it as an MCP client, but make no speedrun.com network calls (a write in
read-only mode is refused before any request), so they run offline in CI.
"""

import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _env(**overrides: str) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("SPEEDRUN_")}
    base.update(overrides)
    return base


def _server(env: dict[str, str]) -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable, args=["-m", "speedrun_mcp"], env=env)


def _text(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


async def test_protocol_lists_public_read_tools_without_a_key():
    async with (
        stdio_client(_server(_env())) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = (await session.list_tools()).tools

    names = {t.name for t in tools}
    assert {"search_games", "get_leaderboard", "get_world_record", "list_unverified_runs"} <= names
    # identity/write tools are not advertised without a key
    assert "whoami" not in names
    assert "submit_run" not in names
    # read tools carry the read-only annotation, surfaced through the protocol
    wr = next(t for t in tools if t.name == "get_world_record")
    assert wr.annotations is not None
    assert wr.annotations.readOnlyHint is True


async def test_protocol_write_tool_is_blocked_in_read_only_mode():
    # A key is set (so write tools register) but writes are NOT enabled, so the
    # call is refused by the guard before any network access.
    async with (
        stdio_client(_server(_env(SPEEDRUN_API_KEY="dummy-key"))) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        names = {t.name for t in (await session.list_tools()).tools}
        assert "submit_run" in names  # visible once a key is present
        result = await session.call_tool(
            "submit_run", {"category": "c", "platform": "p", "realtime": 1.0}
        )

    assert result.isError
    assert "SPEEDRUN_ENABLE_WRITES" in _text(result)
