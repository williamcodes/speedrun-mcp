"""Shape raw speedrun.com payloads into compact, LLM-friendly dicts.

The API returns large, deeply-nested objects full of IDs and HATEOAS links.
These helpers resolve IDs to names, format durations as readable strings, and
drop noise so a model gets the answer instead of the haystack.
"""

from __future__ import annotations

from typing import Any


def format_duration(seconds: float | None) -> str | None:
    """Render a run time in seconds as e.g. ``1h 23m 45.670s``."""
    if seconds is None:
        return None
    total_ms = round(seconds * 1000)
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs = rem / 1000
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or hours:
        parts.append(f"{minutes}m")
    # trim trailing zeros on the seconds component but keep millisecond precision
    secs_str = f"{secs:.3f}".rstrip("0").rstrip(".")
    parts.append(f"{secs_str}s")
    return " ".join(parts)


def _intl_name(named: dict | None) -> str | None:
    """Pull the international name out of a {names: {international: ...}} blob."""
    if not named:
        return None
    names = named.get("names")
    if isinstance(names, dict):
        return names.get("international")
    return named.get("name")


def game_summary(game: dict) -> dict:
    """Compact view of a game resource."""
    return {
        "id": game.get("id"),
        "name": _intl_name(game),
        "abbreviation": game.get("abbreviation"),
        "released": game.get("released"),
        "weblink": game.get("weblink"),
    }


def category_summary(cat: dict) -> dict:
    return {
        "id": cat.get("id"),
        "name": cat.get("name"),
        "type": cat.get("type"),  # "per-game" or "per-level"
        "miscellaneous": cat.get("miscellaneous"),
        "rules": (cat.get("rules") or "").strip() or None,
    }


def variable_summary(var: dict) -> dict:
    """A filterable variable (subcategory like '16 Star', difficulty, etc.)."""
    values = (var.get("values") or {}).get("values") or {}
    scope = var.get("scope") or {}
    summary: dict[str, Any] = {
        "id": var.get("id"),
        "name": var.get("name"),
        "is_subcategory": var.get("is-subcategory"),
        "mandatory": var.get("mandatory"),
        "scope": scope.get("type"),
        # top-level category id this variable is scoped to (null = all categories)
        "category": var.get("category"),
        # map value-id -> label so a caller can pass var-<id>=<value-id> back in
        "values": {vid: meta.get("label") for vid, meta in values.items()},
    }
    # preserve the scoping level id when the scope carries one (e.g. single-level)
    if scope.get("level") is not None:
        summary["level"] = scope.get("level")
    return summary


def _player_name_map(players_block: Any) -> dict[str, str]:
    """Build {user_id: name} from an embedded players list."""
    data = players_block.get("data") if isinstance(players_block, dict) else players_block
    out: dict[str, str] = {}
    for p in data or []:
        if p.get("id"):
            out[p["id"]] = _intl_name(p) or p["id"]
    return out


def _resolve_players(run: dict, name_map: dict[str, str]) -> list[str]:
    """Resolve a run's players to display names.

    Handles all three shapes seen in the wild:
      * leaderboard reference: ``[{"rel": "user", "id": ...}]`` (resolved via name_map)
      * embedded block:        ``{"data": [<full user/guest>]}``
      * guest:                 ``{"rel": "guest", "name": ...}``
    """
    players = run.get("players")
    items = players.get("data") if isinstance(players, dict) else (players or [])
    names: list[str] = []
    for p in items:
        if "names" in p:  # full embedded user object
            names.append(_intl_name(p) or p.get("id", "?"))
        elif p.get("rel") == "guest" or "name" in p:
            names.append(p.get("name", "guest"))
        elif p.get("id"):
            names.append(name_map.get(p["id"], p["id"]))
    return names


def _video_link(run: dict) -> str | None:
    videos = run.get("videos") or {}
    links = videos.get("links")
    if links and isinstance(links, list):
        uri = links[0].get("uri")
        if uri:
            return uri
    # Older runs store the URL in ``videos.text`` instead of a links list.
    text = videos.get("text")
    if text:
        return text
    return None


def run_entry(
    run: dict,
    *,
    place: int | None = None,
    name_map: dict[str, str] | None = None,
    variable_meta: dict[str, dict] | None = None,
    timing: str | None = None,
) -> dict:
    """One leaderboard/PB row, flattened.

    ``name_map`` resolves player ids -> names; ``variable_meta`` maps
    {variable_id: {"name": str, "values": {value_id: label}}} so subcategory
    choices show as readable ``{variable name: value label}`` pairs.

    ``timing`` selects which timing metric to display. When it is a non-empty
    string the time is taken from ``times[f"{timing}_t"]`` (falling back to
    ``times["primary_t"]`` when that value is missing/None/0, since unused
    timings come back as 0). When ``timing`` is None the primary time is used.
    This keeps displayed times in sync with the leaderboard's sort order.
    """
    times = run.get("times") or {}
    primary_t = times.get("primary_t")
    time_seconds = primary_t
    if timing:
        selected = times.get(f"{timing}_t")
        if selected:  # non-None and non-zero
            time_seconds = selected
    entry: dict[str, Any] = {
        "place": place,
        "players": _resolve_players(run, name_map or {}),
        "time": format_duration(time_seconds),
        "time_seconds": time_seconds,
        "date": run.get("date"),
        "video": _video_link(run),
        "run_id": run.get("id"),
        "weblink": run.get("weblink"),
    }
    if variable_meta:
        subcats = {}
        for var_id, value_id in (run.get("values") or {}).items():
            meta = variable_meta.get(var_id)
            if meta:
                label = (meta.get("values") or {}).get(value_id)
                if label:
                    subcats[meta.get("name") or var_id] = label
        if subcats:
            entry["subcategories"] = subcats
    comment = (run.get("comment") or "").strip()
    if comment:
        entry["comment"] = comment
    return {k: v for k, v in entry.items() if v is not None}


def _id_and_name(field: Any) -> tuple[str | None, str | None]:
    """A leaderboard's ``game``/``category`` is a string id, or {"data": {...}}
    when embedded. Return (id, name) for either shape."""
    if isinstance(field, dict):
        data = field.get("data") or {}
        return data.get("id"), data.get("name") or _intl_name(data)
    return field, None


def leaderboard_view(lb: dict, *, limit: int | None = None) -> dict:
    """Flatten a leaderboard (optionally with embedded players/variables/category)."""
    name_map = _player_name_map(lb.get("players", {}))
    variable_meta: dict[str, dict] = {}
    for var in (lb.get("variables") or {}).get("data", []):
        summary = variable_summary(var)
        variable_meta[var["id"]] = {"name": summary["name"], "values": summary["values"]}

    timing = lb.get("timing")
    rows = lb.get("runs") or []
    if limit is not None:
        rows = rows[:limit]
    runs = [
        run_entry(
            r["run"],
            place=r.get("place"),
            name_map=name_map,
            variable_meta=variable_meta,
            timing=timing,
        )
        for r in rows
    ]

    game_id, game_name = _id_and_name(lb.get("game"))
    category_id, category_name = _id_and_name(lb.get("category"))

    # Resolve raw {variable_id: value_id} filters to readable {name: label},
    # falling back to the raw id/value when the variable/value is unknown.
    applied_filters: dict[str, str] = {}
    for var_id, value_id in (lb.get("values") or {}).items():
        meta = variable_meta.get(var_id)
        if meta:
            name = meta.get("name") or var_id
            label = (meta.get("values") or {}).get(value_id) or value_id
            applied_filters[name] = label
        else:
            applied_filters[var_id] = value_id

    view = {
        "game_id": game_id,
        "game_name": game_name,
        "category_id": category_id,
        "category_name": category_name,
        "level": lb.get("level"),
        "timing": timing,
        "applied_filters": applied_filters,
        "weblink": lb.get("weblink"),
        "returned_runs": len(runs),
        "runs": runs,
    }
    return {k: v for k, v in view.items() if v is not None}


def user_summary(user: dict) -> dict:
    loc = (user.get("location") or {}).get("country") or {}
    return {
        "id": user.get("id"),
        "name": _intl_name(user),
        "country": _intl_name(loc) if loc else None,
        "signup": user.get("signup"),
        "weblink": user.get("weblink"),
    }
