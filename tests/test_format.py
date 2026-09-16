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
    meta = {"varA": {"name": "Platform", "values": {"valX": "N64"}, "is_subcategory": True}}
    entry = fmt.run_entry(run, place=1, name_map={"u1": "Suigi"}, variable_meta=meta)
    assert entry["place"] == 1
    assert entry["players"] == ["Suigi"]
    assert entry["time"] == "14m 35.5s"
    assert entry["video"] == "https://youtu.be/x"
    assert entry["subcategories"]["varA"]["name"] == "Platform"
    assert entry["subcategories"]["varA"]["label"] == "N64"


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


def test_run_entry_never_substitutes_primary_for_unavailable_requested_timing():
    # Zero/missing selected metrics are unavailable, not equal to the primary time.
    run_zero = {"id": "r2", "times": {"primary_t": 875.5, "ingame_t": 0}}
    run_missing = {"id": "r3", "times": {"primary_t": 875.5}}
    for run in (run_zero, run_missing):
        out = fmt.run_entry(run, timing="ingame")
        assert out["time_available"] is False
        assert out["timing"] == "ingame"
        assert out["time_source"] == "times.ingame_t"
        assert out["primary_time_seconds"] == 875.5
        assert "time" not in out
        assert "time_seconds" not in out


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
    assert view["applied_filters"]["variables"]["varA"]["name"] == "Stars"
    assert view["applied_filters"]["variables"]["varA"]["label"] == "16 Star"


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
    assert view["applied_filters"]["variables"]["unknownVar"]["value"] == "unknownVal"


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
            {
                "place": 1,
                "run": {
                    "id": "r1",
                    "players": [{"rel": "user", "id": "u1"}],
                    "times": {"primary_t": 875.5},
                },
            },
            {
                "place": 2,
                "run": {
                    "id": "r2",
                    "players": [{"rel": "guest", "name": "Weegee"}],
                    "times": {"primary_t": 876.42},
                },
            },
        ],
    }
    view = fmt.leaderboard_view(lb)
    assert view["game_name"] == "Super Mario 64"
    assert view["category_name"] == "16 Star"
    assert view["returned_runs"] == 2
    assert view["omitted_from_response"] == 0
    assert "omitted_runs" not in view
    assert "total_runs" not in view  # renamed: returned_runs counts returned rows
    assert [r["place"] for r in view["runs"]] == [1, 2]
    assert view["runs"][0]["players"] == ["Suigi"]
    assert view["runs"][1]["players"] == ["Weegee"]


def test_leaderboard_view_respects_limit():
    lb = {
        "game": "g1",
        "category": "c1",
        "players": {"data": []},
        "variables": {"data": []},
        "runs": [
            {"place": i, "run": {"id": f"r{i}", "times": {"primary_t": float(i)}}}
            for i in range(1, 11)
        ],
    }
    view = fmt.leaderboard_view(lb, limit=3)
    assert len(view["runs"]) == 3
    # returned_runs reflects the number of rows actually returned (bounded by the
    # limit/top + ties), NOT the full leaderboard size.
    assert view["returned_runs"] == 3
    assert view["omitted_from_response"] == 7


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


def test_submission_result_handles_none_for_empty_write_body():
    # A write that answers with an empty body makes the client return None;
    # submission_result must yield {} rather than crash.
    assert fmt.submission_result(None) == {}


def test_resolve_players_tolerates_null_data_block():
    # An unresolvable embed can return {"data": null}; must degrade, not raise.
    assert fmt._resolve_players({"players": {"data": None}}, {}) == []


def test_variable_summary_tolerates_null_value_meta():
    var = {"id": "v1", "name": "Stars", "values": {"values": {"valX": None}}, "scope": {}}
    out = fmt.variable_summary(var)
    assert out["values"] == {"valX": None}  # no crash; label is just None


def test_leaderboard_view_skips_variable_without_id():
    lb = {
        "game": "g1",
        "category": "c1",
        "players": {"data": []},
        "variables": {"data": [{"name": "Stars", "values": {"values": {}}}]},  # no id key
        "values": {},
        "runs": [],
    }
    view = fmt.leaderboard_view(lb)  # must not raise KeyError
    assert view["returned_runs"] == 0


def test_video_link_text_fallback_and_nondict_element():
    # older runs store the URL in videos.text
    assert fmt._video_links({"videos": {"text": "https://old.example/run"}}) == [
        "https://old.example/run"
    ]
    # a bare-string link element is used directly, not .get()'d
    assert fmt._video_links({"videos": {"links": ["https://x.example/v"]}}) == [
        "https://x.example/v"
    ]
    # No video links, and run_entry drops the singular compatibility key.
    assert fmt._video_links({}) == []
    assert "video" not in fmt.run_entry({"id": "r", "times": {"primary_t": 5.0}})


def test_player_details_disambiguate_guests_accounts_and_unresolved_ids():
    run = {
        "players": {
            "data": [
                {"id": "u1", "names": {"international": "Alice"}},
                {"rel": "guest", "name": "Alice"},
                {"rel": "user", "id": "u2"},
            ]
        }
    }
    for out in (fmt.run_entry(run), fmt.submission_result(run)):
        assert out["players"] == ["Alice", "Alice", "u2"]
        assert out["player_details"] == [
            {"type": "user", "id": "u1", "name": "Alice"},
            {"type": "guest", "name": "Alice"},
            {"type": "user", "id": "u2", "name": None},
        ]


def test_unresolved_embedded_name_is_not_mislabeled_as_resolved():
    run = {"players": {"data": [{"id": "u1", "names": {}}]}}
    out = fmt.run_entry(run, name_map=fmt._player_name_map(run["players"]))
    assert out["player_details"][0]["name"] is None
    assert out["players"] == ["u1"]


def test_video_links_are_complete_and_commentary_is_not_a_link():
    run = {
        "videos": {
            "links": [{"uri": "https://example.com/part1"}, {"uri": "https://example.com/part2"}],
            "text": "Two-part recording",
        }
    }
    for out in (fmt.run_entry(run), fmt.submission_result(run)):
        assert out["videos"] == ["https://example.com/part1", "https://example.com/part2"]
        assert out["video_text"] == "Two-part recording"
    text_only = fmt.run_entry({"videos": {"text": "Ask the moderator for evidence."}})
    assert "video" not in text_only
    assert text_only["videos"] == []
    assert text_only["video_text"] == "Ask the moderator for evidence."


def test_variable_summary_preserves_submission_requirements():
    var = {
        "id": "v1",
        "name": "Route",
        "user-defined": True,
        "obsoletes": False,
        "mandatory": True,
        "values": {
            "default": "x1",
            "values": {
                "x1": {"label": "Easy", "rules": "No assists", "flags": {"miscellaneous": True}}
            },
        },
    }
    out = fmt.variable_summary(var)
    assert out["default"] == "x1"
    assert out["user_defined"] is True
    assert out["obsoletes"] is False
    assert out["value_details"]["x1"] == var["values"]["values"]["x1"]


def test_only_subcategory_variables_are_labeled_as_subcategories():
    meta = {
        "v1": {"name": "Choice", "values": {"a": "Easy"}, "is_subcategory": True},
        "v2": {"name": "Choice", "values": {"b": "English"}, "is_subcategory": False},
    }
    out = fmt.run_entry({"values": {"v1": "a", "v2": "b", "unknown": "raw"}}, variable_meta=meta)
    assert set(out["subcategories"]) == {"v1"}
    assert out["variables"]["v2"]["label"] == "English"
    assert out["variables"]["unknown"]["value"] == "raw"
    assert out["values"] == {"v1": "a", "v2": "b", "unknown": "raw"}


def test_leaderboard_preserves_system_filters_and_requested_date():
    view = fmt.leaderboard_view(
        {"platform": "p1", "region": "r1", "emulators": False, "values": {}, "runs": []},
        requested_filters={"date": "2020-01-01", "platform": "p1"},
    )
    assert view["applied_filters"] == {
        "platform": "p1",
        "region": "r1",
        "emulators": False,
        "variables": {},
    }
    assert view["requested_filters"]["date"] == "2020-01-01"


def test_notification_preserves_target_uri_and_notification_id_separately():
    out = fmt.notification_view(
        {"id": "n1", "item": {"rel": "post", "uri": "https://www.speedrun.com/post/p1"}}
    )
    assert out["id"] == "n1"
    assert out["target_uri"] == "https://www.speedrun.com/post/p1"


def test_category_preserves_player_count_requirement():
    assert fmt.category_summary({"players": {"type": "exactly", "value": 2}})["players"] == {
        "type": "exactly",
        "value": 2,
    }


def test_run_detail_uses_shared_variable_formatting_and_skips_incomplete_metadata():
    run = {"id": "r1", "values": {"v1": "x1", "missing": "raw"}}
    variables = [
        {"name": "Incomplete metadata"},
        {
            "id": "v1",
            "name": "Difficulty",
            "is-subcategory": True,
            "values": {"values": {"x1": {"label": "Easy"}}},
        },
    ]

    out = fmt.run_detail(run, variables)

    assert out["subcategories"] == {"v1": out["variables"]["v1"]}
    assert out["subcategories"]["v1"]["label"] == "Easy"
    assert out["variables"]["missing"]["value"] == "raw"
    assert out["values"] == run["values"]
