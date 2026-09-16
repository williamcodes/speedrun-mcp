"""Shape raw speedrun.com payloads into compact, LLM-friendly dicts.

The API returns large, deeply-nested objects full of IDs and HATEOAS links.
These helpers resolve IDs to names, format durations as readable strings, and
drop noise so a model gets the answer instead of the haystack.
"""

from __future__ import annotations

from collections.abc import Callable
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


def series_summary(series: dict) -> dict:
    """Compact view of a series resource (a group of related games)."""
    return {
        "id": series.get("id"),
        "name": _intl_name(series),
        "abbreviation": series.get("abbreviation"),
        "weblink": series.get("weblink"),
    }


def category_summary(cat: dict) -> dict:
    return {
        "id": cat.get("id"),
        "name": cat.get("name"),
        "type": cat.get("type"),  # "per-game" or "per-level"
        "miscellaneous": cat.get("miscellaneous"),
        "rules": (cat.get("rules") or "").strip() or None,
        "players": cat.get("players"),
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
        "values": {vid: (meta or {}).get("label") for vid, meta in values.items()},
        "value_details": values,
        "default": (var.get("values") or {}).get("default"),
        "user_defined": var.get("user-defined"),
        "obsoletes": var.get("obsoletes"),
    }
    # preserve the scoping level id when the scope carries one (e.g. single-level)
    if scope.get("level") is not None:
        summary["level"] = scope.get("level")
    return summary


def _variable_metadata(variables: list[dict]) -> dict[str, dict]:
    """Index usable variable summaries for both individual runs and leaderboards."""
    return {var["id"]: variable_summary(var) for var in variables if var.get("id") is not None}


def _player_name_map(players_block: Any) -> dict[str, str]:
    """Build {user_id: name} from an embedded players list."""
    data = players_block.get("data") if isinstance(players_block, dict) else players_block
    out: dict[str, str] = {}
    for p in data or []:
        name = _intl_name(p)
        if p.get("id") and name:
            out[p["id"]] = name
    return out


def _player_details(run: dict, name_map: dict[str, str]) -> list[dict]:
    """Keep account ids and guest identity separate from display names."""
    players = run.get("players")
    # ``or []`` guards against an embedded block of the form {"data": null},
    # which the API can return for an unresolvable embed.
    items = (players.get("data") if isinstance(players, dict) else players) or []
    details = []
    for p in items:
        if p.get("rel") == "guest" or ("name" in p and not p.get("id")):
            details.append({"type": "guest", "name": p.get("name", "guest")})
        else:
            user_id = p.get("id")
            details.append(
                {"type": "user", "id": user_id, "name": _intl_name(p) or name_map.get(user_id)}
            )
    return details


def _resolve_players(run: dict, name_map: dict[str, str]) -> list[str]:
    """Display labels only; player_details carries the identity/provenance."""
    return [p.get("name") or p.get("id") or "?" for p in _player_details(run, name_map)]


def _video_links(run: dict) -> list[str]:
    videos = run.get("videos") or {}
    urls = []
    for link in videos.get("links") or []:
        uri = link.get("uri") if isinstance(link, dict) else link
        if uri:
            urls.append(uri)
    # Keep prose separate; only use a legacy text field as a link if it is a URL.
    text = videos.get("text")
    if not urls and isinstance(text, str) and text.startswith(("http://", "https://")):
        urls.append(text)
    return urls


def _variable_values(values: dict, metadata: dict[str, dict]) -> dict:
    """Resolve labels without discarding raw choices or treating every variable as a subcategory."""
    return {
        var_id: {
            "name": metadata.get(var_id, {}).get("name"),
            "value": value,
            "label": metadata.get(var_id, {}).get("values", {}).get(value),
            "is_subcategory": metadata.get(var_id, {}).get("is_subcategory"),
        }
        for var_id, value in values.items()
    }


def run_entry(
    run: dict,
    *,
    place: int | None = None,
    name_map: dict[str, str] | None = None,
    variable_meta: dict[str, dict] | None = None,
    timing: str | None = None,
) -> dict:
    """One leaderboard/PB row, flattened.

    ``name_map`` resolves player ids to names; ``variable_meta`` maps variable
    ids to summaries. Choices retain their ids, labels and subcategory flags.

    A missing/zero requested timing is unavailable, never replaced with another
    metric. The primary time is used only when no metric was requested.
    """
    times = run.get("times") or {}
    time_source = f"{timing}_t" if timing else "primary_t"
    time_seconds = times.get(time_source) or None
    videos = _video_links(run)
    game_id, _ = _id_and_name(run.get("game"))
    category_id, _ = _id_and_name(run.get("category"))
    variable_values = _variable_values(run.get("values") or {}, variable_meta or {})
    entry: dict[str, Any] = {
        "place": place,
        "players": _resolve_players(run, name_map or {}),
        "player_details": _player_details(run, name_map or {}),
        "time": format_duration(time_seconds),
        "time_seconds": time_seconds,
        "date": run.get("date"),
        "timing": timing or "primary",
        "time_source": f"times.{time_source}",
        "time_available": time_seconds is not None,
        "primary_time_seconds": times.get("primary_t"),
        "video": videos[0] if videos else None,
        "videos": videos,
        "video_text": (run.get("videos") or {}).get("text"),
        "run_id": run.get("id"),
        "weblink": run.get("weblink"),
        "game_id": game_id,
        "category_id": category_id,
        "values": run.get("values") or {},
        "variables": variable_values,
        "status": run.get("status"),
    }
    subcats = {
        var_id: detail
        for var_id, detail in variable_values.items()
        if detail["is_subcategory"] is True
    }
    if subcats:
        entry["subcategories"] = subcats
    comment = (run.get("comment") or "").strip()
    if comment:
        entry["comment"] = comment
    out = {k: v for k, v in entry.items() if v is not None}
    out["level"] = run.get("level")
    return out


def _id_and_name(field: Any) -> tuple[str | None, str | None]:
    """A leaderboard's ``game``/``category`` is a string id, or {"data": {...}}
    when embedded. Return (id, name) for either shape."""
    if isinstance(field, dict):
        data = field.get("data") or {}
        return data.get("id"), data.get("name") or _intl_name(data)
    return field, None


def run_detail(run: dict, variables: list[dict]) -> dict:
    """A single run with its board identity, verification and submission context."""
    out = run_entry(
        run,
        name_map=_player_name_map(run.get("players", {})),
        variable_meta=_variable_metadata(variables),
    )
    game_id, game_name = _id_and_name(run.get("game"))
    category_id, category_name = _id_and_name(run.get("category"))
    out.update(
        game_id=game_id,
        game_name=game_name,
        category_id=category_id,
        category_name=category_name,
        status=run.get("status"),
        system=run.get("system"),
        submitted=run.get("submitted"),
        times=run.get("times"),
    )
    return out


def leaderboard_view(
    lb: dict, *, limit: int | None = None, requested_filters: dict | None = None
) -> dict:
    """Flatten a leaderboard (optionally with embedded players/variables/category)."""
    name_map = _player_name_map(lb.get("players", {}))
    variable_meta = _variable_metadata((lb.get("variables") or {}).get("data") or [])

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

    applied_filters = {
        key: lb[key] for key in ("platform", "region", "emulators", "date") if key in lb
    }
    applied_filters["variables"] = _variable_values(lb.get("values") or {}, variable_meta)

    view = {
        "game_id": game_id,
        "game_name": game_name,
        "category_id": category_id,
        "category_name": category_name,
        "level": lb.get("level"),
        "timing": timing,
        "applied_filters": applied_filters,
        "requested_filters": {k: v for k, v in (requested_filters or {}).items() if v is not None},
        "weblink": lb.get("weblink"),
        "returned_runs": len(runs),
        "omitted_from_response": max(0, len(lb.get("runs") or []) - len(runs)),
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


def profile_summary(profile: dict) -> dict:
    """The authenticated user (GET /profile): a user summary plus their role."""
    out = user_summary(profile)
    out["role"] = profile.get("role")
    return {k: v for k, v in out.items() if v is not None}


def notification_view(notif: dict) -> dict:
    """One notification flattened to text + read status + any linked run/game."""
    links = {ln.get("rel"): ln.get("uri") for ln in (notif.get("links") or [])}
    item = notif.get("item") or {}
    out = {
        "id": notif.get("id"),
        "status": notif.get("status"),  # "read" | "unread"
        "created": notif.get("created"),
        "text": notif.get("text"),
        "type": item.get("rel"),  # post | run | game | guide
        "target_uri": item.get("uri"),
        "run": links.get("run"),
        "game": links.get("game"),
    }
    return {k: v for k, v in out.items() if v is not None}


def submission_result(run: dict | None, *, name_map: dict[str, str] | None = None) -> dict:
    """Compact view of a run returned by submit/verify/reject/delete or the queue.

    The submit/moderation responses use the *read* run shape, so the time comes
    from ``times.primary_t`` and players may be id-references (resolved via
    ``name_map`` when an embed was requested). A write that answers with an empty
    body (the client returns ``None``) yields ``{}`` — a no-detail success.
    """
    run = run or {}
    status = run.get("status") or {}
    times = run.get("times") or {}
    out: dict[str, Any] = {
        "run_id": run.get("id"),
        "weblink": run.get("weblink"),
        "status": status.get("status"),  # new | verified | rejected
        "examiner": status.get("examiner"),
        "reason": status.get("reason"),  # set on rejection
        "players": _resolve_players(run, name_map or {}),
        "player_details": _player_details(run, name_map or {}),
        "game": run.get("game"),
        "category": run.get("category"),
        "time": format_duration(times.get("primary_t")),
        "date": run.get("date"),
        "submitted": run.get("submitted"),
        "videos": _video_links(run),
        "video_text": (run.get("videos") or {}).get("text"),
    }
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def collection_view(page: dict, formatter: Callable[[dict], dict]) -> dict:
    """An explicit page envelope remains visible even when results are empty."""
    notes = {
        True: "The API advertises a next page; it may contain no matching results.",
        False: "The API advertises no next page.",
        None: "The API omitted pagination metadata; completeness is unknown.",
    }
    return {
        "results": [formatter(item) for item in page["data"]],
        "returned": len(page["data"]),
        "offset": page["offset"],
        "limit": page["limit"],
        "has_more": page["has_more"],
        "next_offset": page["next_offset"],
        "pagination_note": notes[page["has_more"]],
    }


def notification_page(page: dict, *, limit: int, unread_only: bool) -> dict:
    """Filter one fetched page and preserve a continuation before omitted matches."""
    matches = [
        (i, n) for i, n in enumerate(page["data"]) if not unread_only or n.get("status") == "unread"
    ]
    selected = matches[:limit]
    view = collection_view({**page, "data": [n for _, n in selected]}, notification_view)
    if len(matches) > limit:
        view["has_more"] = True
        view["next_offset"] = page["offset"] + selected[-1][0] + 1
        view["pagination_note"] = (
            "More matching notifications in the scanned page were omitted by limit."
        )
    return view
