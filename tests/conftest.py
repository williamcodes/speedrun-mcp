import pytest

from speedrun_mcp import server as s


@pytest.fixture(autouse=True)
def _reset_supporter_state():
    """Keep the per-process supporter cache and notice counter out of tests.

    Status defaults to "unknown" (already checked), which never attaches a
    notice, so unrelated tests see stable results. The notice tests set
    ``s._supporter_checked = False`` to exercise the real lookup.
    """
    saved = (s._supporter, s._supporter_checked, s._notice_calls)
    s._supporter, s._supporter_checked, s._notice_calls = None, True, 0
    yield
    s._supporter, s._supporter_checked, s._notice_calls = saved
