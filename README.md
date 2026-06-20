# speedrun-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server for
[speedrun.com](https://www.speedrun.com). It lets an AI assistant query games,
categories, leaderboards, world records, players and their personal bests —
e.g. *"What's the current Super Mario 64 16-star world record, and who holds it?"*

Built on speedrun.com's official, public [REST API](https://github.com/speedruncomorg/api).
**No account or API key required** (the read endpoints are open); results are
shaped into compact, model-friendly JSON (player ids resolved to names,
durations formatted, subcategory variables labeled).

## Tools

| Tool | What it does |
| --- | --- |
| `search_games` | Fuzzy-search games by name → ids & abbreviations |
| `get_game` | A game's details plus its categories (and optionally levels) |
| `list_categories` | A game's categories (`Any%`, `120 Star`, …) with rules |
| `list_variables` | Subcategory/filter variables and their value ids |
| `list_platforms` / `list_regions` | Platform / region ids for the `platform`/`region` leaderboard filters |
| `get_leaderboard` | A ranked leaderboard (top N; filter by variable / platform / region / timing) |
| `get_world_record` | The current #1 run for a game/category, plus any runs tied for first |
| `search_users` | Find players by username (partial, fuzzy match) |
| `get_user_personal_bests` | A player's PBs across all games |
| `get_run` | Details of a single run |

A typical flow: `search_games` → `list_categories` (and `list_variables` for
subcategories) → `get_leaderboard` / `get_world_record`. Use `list_platforms` /
`list_regions` when you need an id for the `platform` / `region` filters.

## Install & run

Requires Python 3.10+.

```bash
# from PyPI (once published)
pipx install speedrun-mcp        # or: uv tool install speedrun-mcp

# from source
git clone https://github.com/williamcodes/speedrun-mcp
cd speedrun-mcp
pip install -e .
```

The server speaks MCP over stdio:

```bash
speedrun-mcp          # console script
python -m speedrun_mcp # equivalent
```

## Use with Claude Desktop / Claude Code

Add to your MCP client config (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "speedrun": {
      "command": "speedrun-mcp"
    }
  }
}
```

If you installed from source into a virtualenv, point `command` at that
interpreter, e.g. `"command": "/path/to/.venv/bin/speedrun-mcp"`.

For Claude Code:

```bash
claude mcp add speedrun -- speedrun-mcp
```

## Notes & limits

- **Read-only.** Submitting or moderating runs requires an authenticated
  speedrun.com session and is intentionally out of scope.
- **Rate limit:** speedrun.com allows 100 requests/minute per IP and responds
  with HTTP 420 when exceeded; the client surfaces a clear error if you hit it.
- Game and category arguments accept either an id (`o1y9wo6q`) or an
  abbreviation (`sm64`). For precise subcategory leaderboards (e.g. `16 Star`),
  discover the variable/value ids with `list_variables` and pass
  `variables={variable_id: value_id}`.
- **Errors are explanatory.** Invalid ids/filters raise an error that includes
  speedrun.com's own message — e.g. passing a `level` to a full-game category
  returns *"The selected category is for full-game runs, but a level was selected."*

### Output shape

- **Times** reflect the leaderboard's sort timing. When you pass `timing`
  (`realtime` / `realtime_noloads` / `ingame`), the reported `time` /
  `time_seconds` match that ranking, not the game's default timing.
- **`get_leaderboard`** returns `returned_runs` (the number of runs returned,
  bounded by `top` and ties — not the full board size) and a `runs` list with
  resolved player names, formatted times, and labeled subcategories.
- **`get_world_record`** returns `world_record` (the place-1 run, or `null` if
  the board is empty) plus `tied` (a list of any other runs sharing first place).
- **`get_user_personal_bests`** returns `returned` (how many came back, capped by
  `limit`) and `total_available` (the player's true PB count), plus the
  `personal_bests` list with game/category names and resolved players.

## Development

```bash
pip install -e ".[dev]"
pytest -m "not network"   # unit tests (offline)
pytest                    # include live-API tests
ruff check .
```

## License

MIT
