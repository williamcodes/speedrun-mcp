# ⏱️ speedrun-mcp

<!-- mcp-name: io.github.williamcodes/speedrun-mcp -->

[![PyPI version](https://img.shields.io/pypi/v/speedrun-mcp?logo=pypi&logoColor=white)](https://pypi.org/project/speedrun-mcp/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)](https://pypi.org/project/speedrun-mcp/)
[![CI](https://github.com/williamcodes/speedrun-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/williamcodes/speedrun-mcp/actions/workflows/ci.yml)
[![MCP registry](https://img.shields.io/badge/MCP-registry-0098FF)](https://registry.modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A [Model Context Protocol](https://modelcontextprotocol.io) server for
[speedrun.com](https://www.speedrun.com) — let an AI assistant query games,
categories, leaderboards, world records, players and their personal bests, and
(with an API key) submit and moderate runs.

> *"What's the current Super Mario 64 16-star world record, and who holds it?"*

Built on speedrun.com's official [REST API](https://github.com/speedruncomorg/api).
**The read tools need no account or API key** — add a key (see
[Authenticated features](#authenticated-features)) to unlock identity reads and,
optionally, run submission and moderation. Results come back as compact,
model-friendly JSON (player ids resolved to names, durations formatted,
subcategory variables labeled).

## Example

Ask *"the SM64 16-star world record?"* and the model calls `get_world_record`,
which returns resolved JSON. An excerpt:

```json
{
  "game_name": "Super Mario 64",
  "category_name": "16 Star",
  "world_record": {
    "players": ["Suigi"],
    "time": "14m 35.5s",
    "date": "2023-03-22",
    "video": "https://youtu.be/1_vkwkniHuI"
  }
}
```

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
| `get_game_records` | Every category's records for a game in one call (defaults to world records) |
| `search_series` | Fuzzy-search game series (e.g. `Mario`, `Zelda`) |
| `get_series` | A series' details and the games it contains |
| `search_users` | Find players by username (partial, fuzzy match) |
| `get_user_personal_bests` | A player's PBs across all games |
| `get_run` | Details of a single run |
| `list_runs` | Runs filtered by player / game / category / status / examiner |
| `list_unverified_runs` | A game's runs awaiting verification (the moderation queue) |
| `whoami` | The profile that owns your API key *(only shown when a key is set)* |
| `list_notifications` | Your speedrun.com notifications *(only shown when a key is set)* |

A typical flow: `search_games` → `list_categories` (and `list_variables` for
subcategories) → `get_leaderboard` / `get_world_record`. Use `list_platforms` /
`list_regions` when you need an id for the `platform` / `region` filters.

With write tools enabled (see below), `submit_run`, `verify_run`, `reject_run`,
`set_run_players` and `delete_run` are also available.

### Result scope and provenance

Search tools, `list_runs`, `list_unverified_runs`, and `list_notifications` return
an object with `results`, `returned`, `offset`, `limit`, `has_more`, and
`next_offset`. This replaces their earlier bare-list output. Pass `next_offset`
as `offset` to continue with the same filters. `has_more: null` means the API
omitted pagination metadata, so completeness is unknown. `get_series.games`
uses the same envelope; continue with `game_offset`.
`pagination_note` explains the continuation evidence: an API next-page link
can lead to an empty page and does not establish a total result count.

`get_game_records` follows all pages. Notifications scan up to `scan_limit`
source records and report `scanned`; an empty unread result is not evidence of
no unread notifications when `has_more` is true or unknown.

Run rows preserve account/guest identity in `player_details`, all video links
in `videos`, and separate source commentary in `video_text`. Personal-best rows
include game/category IDs, level, and raw variable choices. Variable details and
subcategory maps are keyed by variable ID to avoid collisions between names.

Leaderboard `applied_filters` includes the API's system filters and resolved
`variables`; `requested_filters` separately records the call's filters, including
historical dates. A missing requested timing is marked unavailable rather than
replaced by the primary time. Each displayed time identifies its source field.
`returned_runs` and `omitted_from_response` count rows from the fetched response,
not the full leaderboard.

Write errors that leave completion uncertain explicitly warn against automatic
retries. A success response that cannot be parsed preserves its HTTP status and
resource location when supplied.

## Install & run

Requires Python 3.10+.

```bash
# from PyPI
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

# with authenticated features (optional):
claude mcp add speedrun \
  -e SPEEDRUN_API_KEY=your-key-here \
  -e SPEEDRUN_ENABLE_WRITES=1 \
  -- speedrun-mcp
```

### One-click install in Claude Desktop

Each [GitHub release](https://github.com/williamcodes/speedrun-mcp/releases)
ships a `speedrun-mcp-<version>.mcpb` bundle. Download it and open it with
Claude Desktop (double-click, or Settings → Extensions → Install from file).
Claude Desktop installs Python and the dependencies itself; nothing else is
needed. The extension settings expose the optional API key and the writes
toggle described below.

## Authenticated features

**An API key is entirely optional.** With no key, the server exposes only the
public read tools (leaderboards, games, players, the moderation queue) and works
exactly as described above — no account required. Adding your key unlocks more:

| Set this env var | Effect |
| --- | --- |
| `SPEEDRUN_API_KEY` | Puts the server in **read-only authenticated mode**. Adds the identity reads — `whoami` (the profile your key belongs to) and `list_notifications`. The write tools (`submit_run`, `verify_run`, `reject_run`, `set_run_players`, `delete_run`) also become *visible*, but stay disabled — calling one returns a message telling you to enable writes. Until a key is set, none of these are advertised at all. |
| `SPEEDRUN_ENABLE_WRITES=1` | Switches to **read-write mode**: arms the write tools so they actually submit/moderate. Requires `SPEEDRUN_API_KEY` (moderation also needs a moderator key). Off by default — submitting and rejecting/deleting are real, permanent actions on real leaderboards, so opt in deliberately. |

**Read-only is the default.** Just adding a key never changes anything on
speedrun.com — you get identity reads, and everything keeps working perfectly. If
a write tool is invoked while writes are off, it doesn't silently fail; it returns:

> *This server is in read-only mode, so this write action is disabled. To allow
> run submission and moderation, set the environment variable
> SPEEDRUN_ENABLE_WRITES=1 (alongside SPEEDRUN_API_KEY) and restart the server.*

So the way to switch to read-write mode is always discoverable from the error
itself.

### Getting your API key

1. Log in to [speedrun.com](https://www.speedrun.com).
2. Go to your account **settings**.
3. In the left-hand nav, find the **Developers** section and click **API Key**.
4. Copy the key shown there.

Treat the key like a password — anyone who has it can act as you on
speedrun.com. If it ever leaks, regenerate it from that same page.

### Using your key

Add the key to your MCP client config under `env`. It is read **only from the
environment** — never passed as a tool argument — so it can't leak into the
model's context or transcripts. Add `SPEEDRUN_ENABLE_WRITES=1` only when you want
writes to actually run; with the key alone you stay safely read-only.

```json
{
  "mcpServers": {
    "speedrun": {
      "command": "speedrun-mcp",
      "env": {
        "SPEEDRUN_API_KEY": "your-key-here",
        "SPEEDRUN_ENABLE_WRITES": "1"
      }
    }
  }
}
```

Or with Claude Code:

```bash
claude mcp add speedrun -e SPEEDRUN_API_KEY=your-key-here -- speedrun-mcp
# add -e SPEEDRUN_ENABLE_WRITES=1 as well if you want the write tools
```

Keep the key out of version control — put it in your client config or a local,
git-ignored `.env`, never in a committed file. All tools carry MCP read-only /
destructive hints so clients can flag the write and moderation actions.

### Local environment file

Copy [.env.example](.env.example) to `.env` and fill in the settings you need.
The template leaves the API key empty and disables writes. Git ignores `.env`.

The server reads exported environment variables and does not load `.env`
automatically. To load your local file and start the server from a shell:

```bash
set -a
. ./.env
set +a
speedrun-mcp
```

## Notes & limits

- **Reads need no key; writes are opt-in.** Leaderboards, games, players and the
  moderation queue are open reads. Run submission and moderation need
  `SPEEDRUN_API_KEY` **and** `SPEEDRUN_ENABLE_WRITES` (see above).
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

The package uses a flat `src/speedrun_mcp/` layout:

- `client.py` handles HTTP requests, API errors, and pagination.
- `format.py` converts API payloads into tool results without network access.
- `server.py` owns MCP tools, configuration, and the shared client's lifecycle.
- `__main__.py` provides the `python -m speedrun_mcp` entry point.
- `__init__.py` exposes `mcp` on demand, so importing the client or format helpers
  does not initialize the server.

Tests live in `tests/`. Unit tests cover each layer; package and MCP protocol
tests cover imports and startup. `test_live.py` contains the live API checks,
selected with the `network` marker.

```bash
pip install -e ".[dev]"
pre-commit install

# The checks run by CI:
ruff check .
ruff format --check .
mypy
pytest -m "not network"

# Apply safe lint fixes and formatting locally:
ruff check --fix .
ruff format .

# Optional: include live API tests.
pytest

# Build the Claude Desktop bundle (needs Node for npx):
npx -y @anthropic-ai/mcpb validate manifest.json
npx -y @anthropic-ai/mcpb pack . speedrun-mcp.mcpb
```

### Releasing

Bump the version in `pyproject.toml`, `server.json` and `manifest.json` (a test
checks they agree), tag `v<version>`, and publish a GitHub release. The release
workflow then runs CI, publishes the wheel to PyPI, attaches the `.mcpb` bundle
to the release, and publishes the new version to the
[MCP registry](https://registry.modelcontextprotocol.io) with both the PyPI
package and the bundle listed. Registry auth uses GitHub OIDC, so no token is
stored; PyPI verifies ownership through the `mcp-name` comment at the top of
this README.

Ruff checks source and tests for common bugs, security issues, async mistakes,
overly complex functions, and pytest mistakes. It also sorts imports and formats
Python code. Tests may use `assert`; the other lint rules apply to both source
and tests. Print and debugger statements are rejected because the server uses
stdout for the MCP protocol.

Ruff is pinned to the same version in the dev dependencies, its `required-version`
setting, and pre-commit. Update all three together. The hooks apply safe lint fixes
and formatting before running mypy; CI checks without changing files.

## License

MIT
