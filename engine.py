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
import subprocess
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
        print(f"  ⚠️  Search failed for '{query}': {e}", file=sys.stderr)
        return []


def run_searches(queries: list[str], brave_key: str, delay: float = 1.0) -> str:
    """Run all searches and compile results into a single context string."""
    all_results = []
    for i, query in enumerate(queries):
        print(f"  🔍 [{i+1}/{len(queries)}] {query}", file=sys.stderr)
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

def sanitize_html(text: str) -> str:
    """
    Fix malformed HTML for Telegram using a stack-based parser.
    Telegram supports: b, i, u, s, a, code, pre, tg-spoiler.
    Any unclosed or mismatched tags are auto-closed or dropped.
    """
    ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code", "pre", "tg-spoiler"}
    stack: list[str] = []
    result: list[str] = []
    i = 0
    n = len(text)

    while i < n:
        if text[i] != "<":
            result.append(text[i])
            i += 1
            continue

        end = text.find(">", i)
        if end == -1:
            # Unclosed bracket — escape it
            result.append("&lt;")
            i += 1
            continue

        raw = text[i + 1 : end]

        if raw.startswith("/"):
            # Closing tag
            tag_name = raw[1:].strip().split()[0].lower() if raw[1:].strip() else ""
            if tag_name in ALLOWED_TAGS and stack and stack[-1] == tag_name:
                stack.pop()
                result.append(f"</{tag_name}>")
            # else: mismatched/orphan close tag — drop it
        elif raw.startswith("!") or raw.startswith("?"):
            pass  # comments / PI — drop
        else:
            parts = raw.split(None, 1)
            if not parts:
                i = end + 1
                continue
            tag_name = parts[0].lower().rstrip("/")
            attrs_raw = parts[1] if len(parts) > 1 else ""
            self_closing = raw.rstrip().endswith("/")

            if tag_name in ALLOWED_TAGS:
                if tag_name == "a":
                    href_m = re.search(r'href=["\']([^"\']*)["\']', attrs_raw)
                    if href_m:
                        url = href_m.group(1)
                        result.append(f'<a href="{url}">')
                        if not self_closing:
                            stack.append("a")
                    # else: malformed <a> with no href — drop it
                else:
                    result.append(f"<{tag_name}>")
                    if not self_closing:
                        stack.append(tag_name)
            # unknown/disallowed tags are dropped

        i = end + 1

    # Auto-close any still-open tags in reverse order
    for tag in reversed(stack):
        result.append(f"</{tag}>")

    return "".join(result)


def send_telegram(text: str, chat_id: str, thread_id: int, bot_token: str) -> bool:
    """Send message to Telegram. Splits if > 4000 chars."""
    text = sanitize_html(text)
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


def archive_to_github(content: str, module_name: str, run_type: str, repo_path: str) -> None:
    """Archive the generated brief into the local tool-intel repo and push it."""
    try:
        repo_dir = Path(repo_path).expanduser()
        now = datetime.now(tz=TZ)
        today = now.strftime("%Y-%m-%d")
        iso_year, iso_week, _ = now.isocalendar()

        # Match the requested archive layout: daily briefs live in intel/, weekly digests in digests/.
        if run_type == "weekly":
            archive_rel_path = Path("digests") / f"{iso_year}-W{iso_week:02d}-{module_name}.md"
        else:
            archive_rel_path = Path("intel") / f"{today}-{module_name}.md"

        archive_path = repo_dir / archive_rel_path
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_text(content.rstrip() + "\n", encoding="utf-8")

        readme_path = repo_dir / "README.md"
        readme_text = readme_path.read_text(encoding="utf-8")

        # Replace only the Latest Brief section and preserve the rest of the README as-is.
        latest_section = f"## Latest Brief\n\n{content.rstrip()}\n"
        updated_readme = re.sub(
            r"## Latest Brief\n.*?(?=\n---\n|$)",
            latest_section,
            readme_text,
            count=1,
            flags=re.DOTALL,
        )
        if updated_readme == readme_text:
            updated_readme = readme_text.rstrip() + f"\n\n{latest_section}"
        readme_path.write_text(updated_readme, encoding="utf-8")

        commit_message = f"intel: add {module_name} {run_type} brief for {today}"

        # Use git via subprocess so archiving stays independent from the main app flow.
        git_commands = [
            ["git", "add", str(archive_rel_path), "README.md"],
            ["git", "commit", "-m", commit_message],
            ["git", "push", "origin", "main"],
        ]
        for command in git_commands:
            subprocess.run(
                command,
                cwd=repo_dir,
                check=True,
                capture_output=True,
                text=True,
            )

        print(f"📚 Archived brief to GitHub: {archive_rel_path}")
    except Exception as exc:
        # Archiving is explicitly best-effort and must never block Telegram delivery.
        print(f"  ⚠️  GitHub archive failed: {exc}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    module_name = sys.argv[1]
    run_type    = sys.argv[2].lower()   # daily | weekly | flash
    data_only   = "--data-only" in sys.argv

    if run_type not in ("daily", "weekly", "flash"):
        sys.exit(f"❌ Unknown run_type '{run_type}'. Use: daily | weekly | flash")

    print(f"🗞  agent-brief-mac — {module_name} / {run_type}", file=sys.stderr)
    print(f"   {datetime.now(tz=TZ).strftime('%Y-%m-%d %H:%M %Z')}\n", file=sys.stderr)

    # Load config
    config    = load_module_config(module_name)
    brave_key = get_brave_key()

    chat_id   = config["destination"]["chat_id"]
    thread_id = config["destination"]["thread_id"]
    model      = config.get("model", DEFAULT_MODEL)
    max_tokens = config.get("max_tokens", DEFAULT_MAX_TOKENS)

    # Select prompt and search queries
    prompt_path = config["prompts"].get(run_type) or config["prompts"]["daily"]
    prompt_raw  = load_prompt(prompt_path)

    # Substitute date placeholder
    date_str    = datetime.now(tz=TZ).strftime("%B %d, %Y")
    prompt_text = prompt_raw.replace("{date}", date_str)

    queries_key = "searches_weekly" if run_type == "weekly" else "searches"
    queries     = config.get(queries_key) or config.get("searches", [])

    # Run searches
    print(f"🔍 Running {len(queries)} searches...", file=sys.stderr)
    search_context = run_searches(queries, brave_key, delay=1.2)
    print(f"   Search context: {len(search_context)} chars\n", file=sys.stderr)

    if data_only:
        # Output JSON for the cron agent — no Claude call, no Telegram send
        print(json.dumps({
            "module":         module_name,
            "run_type":       run_type,
            "prompt_text":    prompt_text,
            "search_context": search_context,
            "destination":    {"chat_id": chat_id, "thread_id": int(thread_id)},
            "model":          model,
            "max_tokens":     max_tokens,
        }))
        return

    # ── Send-file mode: agent writes content to a file, script sends it ───────
    if "--send-file" in sys.argv:
        idx = sys.argv.index("--send-file")
        file_path = sys.argv[idx + 1]
        content   = Path(file_path).read_text(encoding="utf-8").strip()
        bot_token = get_telegram_token()
        print(f"📤 Sending to Telegram (chat={chat_id}, thread={thread_id})...", file=sys.stderr)
        ok = send_telegram(content, chat_id, thread_id, bot_token)
        if ok:
            github_archive = config.get("github_archive", {})
            if github_archive.get("enabled"):
                archive_to_github(content, module_name, run_type, github_archive["repo_path"])
            print("✅ Sent.", file=sys.stderr)
        else:
            print("❌ Delivery failed", file=sys.stderr)
            sys.exit(1)
        return

    # ── Legacy mode: generate + deliver (kept for manual/debug runs) ──────────
    api_key   = get_anthropic_key()
    bot_token = get_telegram_token()

    print(f"🤖 Generating content with Claude ({model})...", file=sys.stderr)
    content = call_claude(prompt_text, search_context, api_key, model=model, max_tokens=max_tokens)
    print(f"   Generated: {len(content)} chars\n", file=sys.stderr)

    print(f"📤 Sending to Telegram (chat={chat_id}, thread={thread_id})...", file=sys.stderr)
    ok = send_telegram(content, chat_id, thread_id, bot_token)
    if ok:
        github_archive = config.get("github_archive", {})
        if github_archive.get("enabled"):
            print("📚 Archiving brief to GitHub...", file=sys.stderr)
            archive_to_github(content, module_name, run_type, github_archive["repo_path"])
        print("✅ Done!", file=sys.stderr)
    else:
        print("❌ Delivery failed", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
