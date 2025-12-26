# Vox

Discord-facing runtime for the Vyxen Core cognition loop. The bot treats Discord as an I/O surface, keeps bounded memory/state, and routes messages through multiple "realities" (social, moderation, strategy, tooling) before choosing actions.

## Features
- Discord adapter with safety filters, rate limits, and undo journal for admin actions.
- Core cognition loop with bounded SQLite memory, identity vector, and governor scoring.
- Tooling layer for admin-style intents (permissions, roles, macros, FAQs, setup wizard, schedules) with dry-run controls.
- Health/watchdog checks and circuit breakers to keep the runtime safe under load or errors.
- Test suite covering stores, intent parsing, tool reality, and safety primitives.

## Requirements
- Python 3.11+ recommended.
- Discord bot token in `DISCORD_TOKEN`.
- Venice AI key in `VENICE_API_KEY` (used by `vyxen_core.llm`).
- Optional env vars in `RuntimeConfig` (see `vyxen_core/config.py`) to tune tick intervals, memory paths, watchdogs, tool toggles, etc.

## Setup
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# For tests/dev
pip install -r requirements-dev.txt
```
Create a `.env` in the repo root:
```
DISCORD_TOKEN=...
VENICE_API_KEY=...
# Optional: VYXEN_SAFE_MODE_DEFAULT=true to start in safe mode
```

## Running
```bash
# Ensure .venv is activated
./start_vox.sh
# or
python bot.py
```
The adapter logs basic memory usage and will refuse to start without `DISCORD_TOKEN`. Safe Mode defaults can be adjusted via env.

## Testing
```bash
source .venv/bin/activate
pytest
```

## Project Layout
- `bot.py` – entrypoint that loads `.env` and runs the Discord adapter.
- `discord_adapter.py` – Discord client, stimulus/action queues, safety filters, tool execution, undo journal.
- `vyxen_core/` – cognition loop, memory, identity, governor, realities, tool intent parsing, safety/audit helpers.
- `tests/` – unit tests for memory, stores, intent parsing, tools reality, and safety primitives.

## Notes
- `vyxen_core/data/` (SQLite DB and logs) is ignored by git; keep secrets in `.env`.
- Tool execution can run in dry-run mode by default; see `RuntimeConfig` and `ToolsReality` for toggles.
