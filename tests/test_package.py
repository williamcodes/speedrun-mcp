"""Check package imports in fresh interpreters, without pytest's module cache."""

import subprocess
import sys
from textwrap import dedent

import pytest


def _run_python(source: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter and test code; no shell
        [sys.executable, "-I", "-c", dedent(source)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module", ["speedrun_mcp", "speedrun_mcp.client", "speedrun_mcp.format"])
def test_library_import_does_not_initialize_server(module):
    _run_python(
        f"""
        import importlib
        import sys

        importlib.import_module({module!r})
        assert "speedrun_mcp.server" not in sys.modules
        assert "mcp" not in sys.modules
        """
    )


def test_mcp_export_uses_settings_at_server_import():
    _run_python(
        """
        import asyncio
        import os

        os.environ.pop("SPEEDRUN_API_KEY", None)
        os.environ.pop("SPEEDRUN_ENABLE_WRITES", None)
        import speedrun_mcp.client

        os.environ["SPEEDRUN_API_KEY"] = "test-key"
        from speedrun_mcp import mcp
        from speedrun_mcp import server

        assert mcp is server.mcp
        assert mcp is speedrun_mcp.mcp
        assert server.AUTH_ENABLED
        assert not server.WRITES_ENABLED
        names = {tool.name for tool in asyncio.run(mcp.list_tools())}
        assert {"search_games", "whoami", "submit_run"} <= names
        """
    )


def test_unknown_package_attribute_raises_attribute_error():
    _run_python(
        """
        import sys
        import speedrun_mcp

        assert not hasattr(speedrun_mcp, "missing_attribute")
        assert "speedrun_mcp.server" not in sys.modules
        """
    )


def test_release_metadata_versions_agree():
    """pyproject.toml, server.json (MCP registry) and manifest.json (MCPB) must
    carry the same version, since the release workflow publishes all three."""
    import json
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    # A regex rather than tomllib, which only ships with Python 3.11+.
    match = re.search(r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(), re.M)
    assert match, "pyproject.toml has no version"
    pyproject = match.group(1)
    server = json.loads((root / "server.json").read_text())
    manifest = json.loads((root / "manifest.json").read_text())

    assert server["version"] == pyproject
    assert all(p["version"] == pyproject for p in server["packages"])
    assert manifest["version"] == pyproject
