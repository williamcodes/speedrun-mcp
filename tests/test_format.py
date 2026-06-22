"""Offline unit tests for the formatting layer (no network)."""

from speedrun_mcp import format as fmt


def test_format_duration_variants():
    assert fmt.format_duration(None) is None
    assert fmt.format_duration(9.7) == "9.7s"
    assert fmt.format_duration(45) == "45s"
    assert fmt.format_duration(875.5) == "14m 35.5s"
    assert fmt.format_duration(3723.0) == "1h 2m 3s"
    # sub-second precision kept, trailing zeros trimmed
    assert fmt.format_duration(1.230) == "1.23s"


def test_intl_name_handles_shapes():
    assert fmt._intl_name({"names": {"international": "Suigi"}}) == "Suigi"
    assert fmt._intl_name({"name": "England"}) == "England"
    assert fmt._intl_name(None) is None


def test_resolve_players_all_shapes():
    name_map = {"u1": "Alice"}
    # leaderboard reference shape
    assert fmt._resolve_players({"players": [{"rel": "user", "id": "u1"}]}, name_map) == ["Alice"]
    # guest shape
    assert fmt._resolve_players({"players": [{"rel": "guest", "name": "Bob"}]}, {}) == ["Bob"]
    # embedded block of full user objects
    embedded = {"players": {"data": [{"id": "u2", "names": {"international": "Carol"}}]}}
    assert fmt._resolve_players(embedded, {}) == ["Carol"]
    # unresolvable id falls back to the id
    assert fmt._resolve_players({"players": [{"rel": "user", "id": "ux"}]}, {}) == ["ux"]


def test_run_entry_resolves_subcategories_by_variable_name():
    run = {
        "id": "r1",
        "players": [{"rel": "user", "id": "u1"}],
        "times": {"primary_t": 875.5},
        "date": "2023-03-22",
        "values": {"varA": "valX"},
        "videos": {"links": [{"uri": "https://youtu.be/x"}]},
    }
    meta = {"varA": {"name": "Platform", "values": {"valX": "N64"}}}
    entry = fmt.run_entry(run, place=1, name_map={"u1": "Suigi"}, variable_meta=meta)
    assert entry["place"] == 1
    assert entry["players"] == ["Suigi"]
    assert entry["time"] == "14m 35.5s"
    assert entry["video"] == "https://youtu.be/x"
    assert entry["subcategories"] == {"Platform": "N64"}


def test_run_entry_timing_selects_named_metric():
    # ingame is the primary; realtime is a separate, slower metric.
    run = {
        "id": "r1",
        "times": {"primary_t": 50.0, "realtime_t": 53.755, "ingame_t": 50.0},
    }
    # default (no timing) -> primary
    default = fmt.run_entry(run)
    assert default["time_seconds"] == 50.0
    # explicit timing -> the named *_t field
    realtime = fmt.run_entry(run, timing="realtime")
    assert realtime["time_seconds"] == 53.755
    assert realtime["time"] == "53.755s"


def test_run_entry_timing_falls_back_to_primary_when_zero_or_missing():
    # unused timings come back as 0 (or are absent) -> fall back to primary_t.
    run_zero = {"id": "r2", "times": {"primary_t": 875.5, "ingame_t": 0}}
    assert fmt.run_entry(run_zero, timing="ingame")["time_seconds"] == 875.5

    run_missing = {"id": "r3", "times": {"primary_t": 875.5}}
    assert fmt.run_entry(run_missing, timing="realtime")["time_seconds"] == 875.5


def test_leaderboard_view_resolves_applied_filters_to_labels():
    lb = {
        "game": "g1",
        "category": "c1",
        "players": {"data": []},
        "variables": {
            "data": [
                {
                    "id": "varA",
                    "name": "Stars",
                    "values": {"values": {"valX": {"label": "16 Star"}}},
                }
            ]
        },
        # raw filter the API echoes back as {variable_id: value_id}
        "values": {"varA": "valX"},
        "runs": [],
    }
    view = fmt.leaderboard_view(lb)
    # resolved to readable {name: label}, not raw ids
    assert view["applied_filters"] == {"Stars": "16 Star"}


def test_leaderboard_view_applied_filters_falls_back_to_raw_ids():
    lb = {
        "game": "g1",
        "category": "c1",
        "players": {"data": []},
        "variables": {"data": []},  # no metadata to resolve against
        "values": {"unknownVar": "unknownVal"},
        "runs": [],
    }
    view = fmt.leaderboard_view(lb)
    assert view["applied_filters"] == {"unknownVar": "unknownVal"}


def test_id_and_name_handles_string_and_embedded():
    assert fmt._id_and_name("abc123") == ("abc123", None)
    embedded = {"data": {"id": "abc123", "name": "Any%"}}
    assert fmt._id_and_name(embedded) == ("abc123", "Any%")


def test_leaderboard_view_flattens_and_orders():
    lb = {
        "game": {"data": {"id": "g1", "names": {"international": "Super Mario 64"}}},
        "category": {"data": {"id": "c1", "name": "16 Star"}},
        "timing": "realtime",
        "weblink": "https://example.com",
        "players": {"data": [{"id": "u1", "names": {"international": "Suigi"}}]},
        "variables": {"data": []},
        "runs": [
            {"place": 1, "run": {"id": "r1", "players": [{"rel": "user", "id": "u1"}],
                                 "times": {"primary_t": 875.5}}},
            {"place": 2, "run": {"id": "r2", "players": [{"rel": "guest", "name": "Weegee"}],
                                 "times": {"primary_t": 876.42}}},
        ],
    }
    view = fmt.leaderboard_view(lb)
    assert view["game_name"] == "Super Mario 64"
    assert view["category_name"] == "16 Star"
    assert view["returned_runs"] == 2
    assert "total_runs" not in view  # renamed: returned_runs counts returned rows
    assert [r["place"] for r in view["runs"]] == [1, 2]
    assert view["runs"][0]["players"] == ["Suigi"]
    assert view["runs"][1]["players"] == ["Weegee"]


def test_leaderboard_view_respects_limit():
    lb = {
        "game": "g1", "category": "c1",
        "players": {"data": []}, "variables": {"data": []},
        "runs": [{"place": i, "run": {"id": f"r{i}", "times": {"primary_t": float(i)}}}
                 for i in range(1, 11)],
    }
    view = fmt.leaderboard_view(lb, limit=3)
    assert len(view["runs"]) == 3
    # returned_runs reflects the number of rows actually returned (bounded by the
    # limit/top + ties), NOT the full leaderboard size.
    assert view["returned_runs"] == 3


def test_series_summary_compact():
    series = {
        "id": "s1",
        "names": {"international": "Super Mario"},
        "abbreviation": "smario",
        "weblink": "https://www.speedrun.com/smario",
    }
    assert fmt.series_summary(series) == {
        "id": "s1",
        "name": "Super Mario",
        "abbreviation": "smario",
        "weblink": "https://www.speedrun.com/smario",
    }


def test_profile_summary_includes_role():
    profile = {
        "id": "u1",
        "names": {"international": "Suigi"},
        "weblink": "https://www.speedrun.com/user/Suigi",
        "role": "user",
        "location": {"country": {"names": {"international": "Japan"}}},
        "signup": "2014-01-01T00:00:00Z",
    }
    out = fmt.profile_summary(profile)
    assert out["id"] == "u1"
    assert out["name"] == "Suigi"
    assert out["role"] == "user"
    assert out["country"] == "Japan"


def test_notification_view_flattens_links_and_status():
    notif = {
        "id": "n1",
        "status": "unread",
        "created": "2015-01-25T11:55:15Z",
        "text": "Foo verified your run.",
        "item": {"rel": "run", "uri": "https://x"},
        "links": [
            {"rel": "run", "uri": "https://www.speedrun.com/api/v1/runs/r1"},
            {"rel": "game", "uri": "https://www.speedrun.com/api/v1/games/g1"},
        ],
    }
    out = fmt.notification_view(notif)
    assert out["status"] == "unread"
    assert out["type"] == "run"
    assert out["run"].endswith("/runs/r1")
    assert out["game"].endswith("/games/g1")


def test_submission_result_is_compact_and_drops_empties():
    run = {
        "id": "r1",
        "weblink": "https://www.speedrun.com/run/r1",
        "status": {"status": "rejected", "reason": "fake", "examiner": "m1"},
        "players": [{"rel": "user", "id": "u1"}],
        "game": "g1",
        "category": "c1",
        "times": {"primary_t": 92.5},
        "date": "2023-01-01",
        # no "submitted" -> the key should be dropped from the result
    }
    out = fmt.submission_result(run, name_map={"u1": "Suigi"})
    assert out["run_id"] == "r1"
    assert out["status"] == "rejected"
    assert out["reason"] == "fake"
    assert out["players"] == ["Suigi"]
    assert out["time"] == "1m 32.5s"
    assert "submitted" not in out
