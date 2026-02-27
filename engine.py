#!/usr/bin/env python3
"""
agent-brief-mac — Configured Brief Engine

Usage:
  python engine.py <module> <run_type>

  module:    name of config in configs/ (e.g. stone-news)
  run_type:  daily | weekly | flash

Examples:
  python engine.py stone-news daily
  python engine.py stone-news weekly
  python engine.py stone-news flash
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
TZ = ZoneInfo("America/Los_Angeles")

# ── Config loaders ────────────────────────────────────────────────────────────

def load_module_config(module_name: str) -> dict:
    config_path = SCRIPT_DIR / "configs" / f"{module_name}.yaml"
    if not config_path.exists():
        sys.exit(f"❌ Config not found: {config_path}")
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_prompt(prompt_path: str) -> str:
    full_path = SCRIPT_DIR / prompt_path
    if not full_path.exists():
        sys.exit(f"❌ Prompt not found: {full_path}")
    return full_path.read_text()


def get_anthropic_key() -> str:
    auth_path = Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth.json"
    if auth_path.exists():
        try:
            key = json.loads(auth_path.read_text()).get("anthropic", {}).get("key")
            if key:
                return key
        except Exception:
            pass
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        sys.exit("❌ No Anthropic API key found")
    return key


def get_brave_key() -> str:
    config_path = Path.home() / ".openclaw" / "openclaw.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
            key = cfg.get("tools", {}).get("web", {}).get("search", {}).get("apiKey", "")
            if key:
                return key
        except Exception:
            pass
    key = os.environ.get("BRAVE_API_KEY", "")
    if not key:
        sys.exit("❌ No Brave API key found")
    return key


def get_telegram_token() -> str:
    config_path = Path.home() / ".openclaw" / "openclaw.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
            token = cfg.get("channels", {}).get("telegram", {}).get("botToken", "")
            if token:
                return token
        except Exception:
            pass
    sys.exit("❌ No Telegram bot token found")


# ── Web search ────────────────────────────────────────────────────────────────

def web_search(query: str, brave_key: str, count: int = 8) -> list[dict]:
    """Run a Brave web search. Returns list of {title, url, description}."""
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"Accept": "application/json", "X-Subscription-Token": brave_key},
            params={"q": query, "count": count, "freshness": "pd"},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("web", {}).get("results", [])
        return [{"title": r.get("title", ""), "url": r.get("url", ""), "description": r.get("description", "")} for r in results]
    except Exception as e:
        print(f"  ⚠️  Search failed for '{query}': {e}")
        return []


def run_searches(queries: list[str], brave_key: str, delay: float = 1.0) -> str:
    """Run all searches and compile results into a single context string."""
    all_results = []
    for i, query in enumerate(queries):
        print(f"  🔍 [{i+1}/{len(queries)}] {query}")
        results = web_search(query, brave_key)
        for r in results:
            all_results.append(f"• [{r['title']}]({r['url']})\n  {r['description']}")
        if i < len(queries) - 1:
            time.sleep(delay)

    return "\n\n".join(all_results) if all_results else "No search results found."


# ── AI generation ─────────────────────────────────────────────────────────────

DEFAULT_MODEL      = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS = 4096


def call_claude(prompt: str, search_context: str, api_key: str,
                model: str = DEFAULT_MODEL, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
    """Call Claude with the prompt + search results. Returns generated text."""
    now = datetime.now(tz=TZ)
    date_str = now.strftime("%B %d, %Y")

    full_prompt = prompt.replace("{date}", date_str)
    full_prompt += f"\n\n---\n## Search Results\n\n{search_context}"

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": full_prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()


# ── Telegram delivery ─────────────────────────────────────────────────────────

def send_telegram(text: str, chat_id: str, thread_id: int, bot_token: str) -> bool:
    """Send message to Telegram. Splits if > 4000 chars."""
    MAX_LEN = 4000
    chunks = []
    while len(text) > MAX_LEN:
        split_at = text.rfind("\n", 0, MAX_LEN)
        if split_at == -1:
            split_at = MAX_LEN
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)

    for chunk in chunks:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "message_thread_id": thread_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if not resp.ok:
            print(f"  ⚠️  Telegram error: {resp.text}")
            return False
        time.sleep(0.5)

    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    module_name = sys.argv[1]
    run_type    = sys.argv[2].lower()   # daily | weekly | flash

    if run_type not in ("daily", "weekly", "flash"):
        sys.exit(f"❌ Unknown run_type '{run_type}'. Use: daily | weekly | flash")

    print(f"🗞  agent-brief-mac — {module_name} / {run_type}")
    print(f"   {datetime.now(tz=TZ).strftime('%Y-%m-%d %H:%M %Z')}\n")

    # Load config and credentials
    config     = load_module_config(module_name)
    brave_key  = get_brave_key()
    api_key    = get_anthropic_key()
    bot_token  = get_telegram_token()

    chat_id   = config["destination"]["chat_id"]
    thread_id = config["destination"]["thread_id"]

    # Select prompt and search queries
    prompt_path = config["prompts"].get(run_type) or config["prompts"]["daily"]
    prompt      = load_prompt(prompt_path)

    queries_key = "searches_weekly" if run_type == "weekly" else "searches"
    queries     = config.get(queries_key) or config.get("searches", [])

    # Run searches
    print(f"🔍 Running {len(queries)} searches...")
    search_context = run_searches(queries, brave_key, delay=1.2)
    print(f"   Search context: {len(search_context)} chars\n")

    # Generate content (model from config, with fallback to engine default)
    model      = config.get("model", DEFAULT_MODEL)
    max_tokens = config.get("max_tokens", DEFAULT_MAX_TOKENS)
    print(f"🤖 Generating content with Claude ({model})...")
    content = call_claude(prompt, search_context, api_key, model=model, max_tokens=max_tokens)
    print(f"   Generated: {len(content)} chars\n")

    # Deliver
    print(f"📤 Sending to Telegram (chat={chat_id}, thread={thread_id})...")
    ok = send_telegram(content, chat_id, thread_id, bot_token)
    if ok:
        print("✅ Done!")
    else:
        print("❌ Delivery failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
