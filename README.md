# agent-brief-mac

Configured brief engine for Mac Mini M4. Runs scheduled and on-demand content briefs.

## Architecture

```
engine.py              # Core runtime: args → config → search → AI → Telegram
configs/               # One YAML per module
prompts/               # Prompt files referenced by configs
```

## Usage

```bash
python engine.py <module> <run_type>

# run_type: daily | weekly | flash
python engine.py stone-news daily
python engine.py stone-news weekly
python engine.py stone-news flash   # on-demand
```

## Modules

| Module | Command | Topics | Schedule |
|--------|---------|--------|----------|
| stone-news | `/digest stone-news` | 8 | Daily 6:15am, Weekly Fri 5pm |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Requires: Brave API key + Anthropic key + Telegram bot token (all read from OpenClaw config).

## Adding a Module

1. Create `configs/<name>.yaml` with trigger, destination, prompts, searches
2. Create `prompts/<name>-<type>.md` with the AI prompt
3. Add cron jobs via OpenClaw
