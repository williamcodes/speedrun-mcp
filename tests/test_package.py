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
