# Bug & Improvement Backlog

Consolidated from three independent reviews (Claude, Codex, Gemini), deduplicated
and verified against the live speedrun.com API. Ordered by severity.

**Status (2026-06-19): original 17 fixed + 1 from a 2nd review round (see below).**
Most via a parallel workflow (one agent per file) + a parallel adversarial-verification
pass; L8 + the round-2 item fixed afterward. **22/22 tests pass, ruff clean,** all
High/Medium fixes confirmed against the live API.

Legend: source tags `[C]`=Claude `[X]`=Codex `[G]`=Gemini · status `[x]` done / `[ ]` open

---

## 🔴 HIGH

- [x] **H1 — Displayed run times used the wrong timing metric when `timing` was overridden.** `[C]`
  - **Was:** `run_entry` hardcoded `times["primary_t"]`; `leaderboard_view` never threaded `lb["timing"]`. Confirmed: Minecraft "Any% Glitchless" + `timing=realtime` showed 4th place faster than 2nd.
  - **Fixed:** `run_entry(..., timing=None)`; selects `times[f"{timing}_t"]`, falls back to `primary_t` when `0`/missing. `leaderboard_view` passes `lb["timing"]`. **Verified live:** times now `monotonic_with_place: True`.

## 🟠 MEDIUM

- [x] **M1 — `get_user_personal_bests` didn't resolve player IDs to names.** `[X][G]` — now embeds `players`; per-item `name_map`. **Verified:** PBs → `['Suigi', …]` not `jn32931x`.
- [x] **M2 — `search_users` couldn't do partial search (exact-only `lookup`).** `[X]` — switched to `name`. **Verified:** `search_users("sui")` → `sui_0x0, Sui_ka, …`.
- [x] **M3 — `total_runs` was misleading.** `[C][X]` — renamed `returned_runs`; documented as the returned count, not the board size.
- [x] **M4 — `get_user_personal_bests` truncated silently; `count` was misleading.** `[C]` — now returns `returned` + `total_available` (true count from the unpaginated list); `count` removed.
- [x] **M5 — API error `message` body discarded on 4xx.** `[G]` — `_get` now surfaces the API's `message`. **Verified:** level-on-full-game → *"The selected category is for full-game runs, but a level was selected."*
- [x] **M6 — `get_world_record` silently dropped tied WRs.** `[C]` — returns `world_record` + `tied` list (drops the `limit=1` so ties survive).

## 🟡 LOW

- [x] **L1 — `get_world_record` empty case emitted contradictory fields.** `[C]` — empty → `world_record=None`, `tied=[]`, no stray `runs`.
- [x] **L2 — Non-JSON `200`/error bodies raised uncaught `JSONDecodeError`.** `[C]` — guarded `.json()` on both paths.
- [x] **L3 — `_video_link` dropped `videos.text` (legacy).** `[C]` — falls back to `videos["text"]`.
- [x] **L4 — Leaderboard/WR output omitted `game_name`.** `[C]` — added `game` to the embed. **Verified:** `game_name: "Super Mario 64"`.
- [x] **L5 — `applied_filters` shown as raw IDs.** `[C]` — resolved to `{variable_name: value_label}` with raw-id fallback.
- [x] **L6 — Variable scoping lost.** `[X][C]` — `variable_summary` now preserves the scoping `category` id (and `level` when present).
- [x] **L7 — `platform`/`region` filters not discoverable.** `[X]` — added `list_platforms` / `list_regions` tools (+ client methods). **Verified:** 11 tools registered; `list_platforms` returns `{id, name}`.
- [x] **L8 — httpx client never closed.** `[C]` — added a FastMCP `lifespan` that closes the shared client on shutdown. Covered by `tests/test_server.py::test_lifespan_closes_shared_client`.

## 🧰 ENHANCEMENTS / REFACTORS

- [x] **E1 — Specialized exceptions.** `[G]` — added `NotFoundError(SpeedrunError)` for 404 (alongside `RateLimitError`).
- [x] **E2 — Test coverage.** `[G][C]` — added 9 tests: timing selection + fallback, applied-filters resolution, PB name resolution, partial user search, 4xx-message surfacing, IL leaderboard with a level id, WR `tied` shape. (The timing-override test would have caught H1.)

## 🔁 ROUND 2 — re-review of the patched code (Codex + Gemini)

- [x] **R1 — `get_platforms` truncated at 200 of ~235 (no pagination).** `[X]` (Codex)
  - **Was:** `_get("/platforms", {"max": 200})` returned only page 1; platforms sorting after
    ~"TurboGrafx-16 Mini" (Wii, Xbox, Virtual Boy, …) were missing from `list_platforms`.
  - **Fixed:** added `_request` + `_get_paginated` (follows `pagination.links` `next` / offset);
    `get_platforms`/`get_regions` now page through fully. **Verified live:** 235 platforms,
    Wii/Xbox/Virtual Boy present. Test: `test_list_platforms_paginates_beyond_one_page`.

- **Rejected — `get_world_record` `top=1` "drops tied WRs".** `[G]` (Gemini) — FALSE POSITIVE.
  speedrun.com's `top` is *place-based*, not run-based: confirmed live that a category with two
  runs tied at place 5 returns BOTH under `top=5`, so `top=1` returns all place-1 runs incl. ties.
  The cited category (SMO `w20w1lzd`) has exactly 1 run at place 1 (the "321" was the total-run
  count, not ties), so `tied: []` was correct. Removing `top=1` would download the entire board
  (5230 runs) for no benefit — kept as-is.

- **Already fixed — README missing `list_platforms`/`list_regions`.** `[G]` (Gemini) — added to
  the README (tools table + flow note) in the prior turn; no action needed.

## 📝 NOTES (not bugs)
- Dead code still present: `client.get_levels`, `get_user`, `get_category_variables` (unused).
- Placeholder repo URL `williamcodes/speedrun-mcp` in `USER_AGENT` + `pyproject.toml` (awaiting the real repo name).
- Tool count is now **11** (added `list_platforms`, `list_regions`).
