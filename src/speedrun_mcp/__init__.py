"""speedrun-mcp: a Model Context Protocol server for the speedrun.com API.

Importing the client or format helpers does not initialize the server. The
package-level ``mcp`` export loads the server when it is first requested.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from .server import mcp as mcp

__all__ = ["mcp"]


def __getattr__(name: str) -> FastMCP:
    if name == "mcp":
        from .server import mcp

        globals()[name] = mcp
        return mcp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
