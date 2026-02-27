#!/usr/bin/env python3
"""
Birthday Runner — agent-brief-mac

Checks CRM database for contacts with birthdays today (score > 30).
Sends one Telegram message per contact to the CRM topic with:
  - Humanized birthday message (Claude)
  - 📋 Copy message button
  - 💬 iMessage button (pre-filled, if phone available)

Usage:
  python birthday_runner.py                  # run for today
  python birthday_runner.py --test-date 09-06   # run for a specific MM-DD (testing)
"""

import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

# ── Config ────────────────────────────────────────────────────────────────────

SCRIPT_DIR   = Path(__file__).parent
CONFIG_FILE  = SCRIPT_DIR / "configs" / "birthdays.yaml"
TZ           = ZoneInfo("America/Los_Angeles")
CRM_DB       = Path("/Users/antonio/dev/Agents/projects/crm/data/crm.db")

# ── Load config ───────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit(f"❌ Config not found: {CONFIG_FILE}")
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


# ── Credentials ───────────────────────────────────────────────────────────────

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


# ── CRM queries ───────────────────────────────────────────────────────────────

def get_birthday_contacts(db_path: Path, mmdd: str, min_score: int) -> list[dict]:
    """Return contacts with birthday on mmdd (MM-DD) and score > min_score."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT
            c.id, c.name, c.email, c.phone, c.company, c.role,
            c.score, c.birthday, c.last_touch, c.last_topic,
            c.preferred_name, c.relationship_type, c.how_we_met,
            c.interaction_count_30d, c.interaction_count_90d
        FROM contacts c
        WHERE c.birthday IS NOT NULL
          AND c.birthday != ''
          AND substr(c.birthday, 6, 5) = ?
          AND c.score > ?
        GROUP BY c.email          -- deduplicate contacts imported from multiple sources
        ORDER BY c.score DESC
    """, (mmdd, min_score))

    contacts = []
    for row in cur.fetchall():
        c = dict(row)
        # Get last interaction snippet
        cur2 = conn.cursor()
        cur2.execute("""
            SELECT date, subject, snippet, source FROM interactions
            WHERE contact_id = ?
            ORDER BY date DESC LIMIT 1
        """, (c["id"],))
        last = cur2.fetchone()
        c["last_interaction"] = dict(last) if last else None
        contacts.append(c)

    conn.close()
    return contacts


# ── Phone normalisation ───────────────────────────────────────────────────────

def normalize_phone(phone: str | None) -> str | None:
    """Strip formatting, return digits only with + prefix if international."""
    if not phone:
        return None
    digits = re.sub(r"[^\d+]", "", phone)
    # Remove leading + for length check
    bare = digits.lstrip("+")
    if len(bare) == 10:
        return f"+1{bare}"          # US number without country code
    if len(bare) >= 11:
        return f"+{bare}"
    return None


def tel_url(phone: str) -> str:
    """Build a tel: URL — tapping on iPhone opens iMessage/Phone."""
    return f"tel:{phone}"


# ── AI message generation ─────────────────────────────────────────────────────

def generate_birthday_message(contact: dict, api_key: str, model: str) -> str:
    """Generate a short, humanized birthday message for the contact."""
    name        = contact.get("preferred_name") or contact.get("name", "").split()[0]
    company     = contact.get("company") or ""
    role        = contact.get("role") or ""
    rel_type    = contact.get("relationship_type") or ""
    how_met     = contact.get("how_we_met") or ""
    last_touch  = contact.get("last_touch") or "unknown"
    last_topic  = contact.get("last_topic") or ""
    score       = contact.get("score", 50)
    last_int    = contact.get("last_interaction")

    last_int_str = ""
    if last_int:
        last_int_str = f"Last interaction ({last_int['date']}): {last_int.get('subject','')}"

    prompt = f"""Write a short, warm birthday message that Antonio Silveira (CTO at Attentive) could send to {name}.

Contact context:
- Name: {name}
- Company/Role: {role} at {company}
- Relationship type: {rel_type}
- How they met: {how_met}
- CRM score: {score}/100
- Last contact: {last_touch}
- {last_int_str}

Rules:
- 1-3 sentences max — keep it short and genuine
- First-person, direct, sounds like a real person not a bot
- Include 1-2 birthday emojis naturally (🎂 🎉 🥂 🎈)
- Reference something personal/professional if context allows
- No AI vocabulary: no "I hope this message finds you", "wishing you all the best", "leverage", "foster", "crucial", "ensure"
- No "Happy Birthday [Name]!" as the opener — be more creative
- Sound like Antonio — warm but concise, CTO energy
- Return ONLY the message text, nothing else"""

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()


# ── Telegram delivery ─────────────────────────────────────────────────────────

def html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_birthday_message(contact: dict, message: str, chat_id: str,
                          thread_id: int, bot_token: str) -> bool:
    """Send one birthday card message with inline buttons."""
    name       = contact.get("name", "Unknown")
    company    = contact.get("company") or ""
    role       = contact.get("role") or ""
    score      = contact.get("score", 0)
    last_touch = contact.get("last_touch") or "never"
    phone_raw  = contact.get("phone")
    phone      = normalize_phone(phone_raw)

    # Build birthday year display
    bday = contact.get("birthday", "")
    bday_display = ""
    if bday:
        try:
            year = int(bday[:4])
            mmdd = bday[5:]
            if year > 1800:
                today_year = date.today().year
                age = today_year - year
                bday_display = f" · 🎂 Turns {age} today"
            else:
                bday_display = ""
        except Exception:
            pass

    # Context line
    context_parts = []
    if role and company:
        context_parts.append(f"{role} at {company}")
    elif company:
        context_parts.append(company)
    context_line = " · ".join(filter(None, [
        html_escape(", ".join(context_parts)),
        f"Score {score}/100",
        f"Last contact: {last_touch}{bday_display}",
    ]))

    # Phone line — tappable on iPhone (opens iMessage/Phone)
    phone_line = ""
    if phone:
        phone_line = f"\n📱 <a href=\"{tel_url(phone)}\">{html_escape(phone_raw or phone)}</a>"

    text = (
        f"🎂 <b>{html_escape(name)}</b> has a birthday today\n"
        f"<i>{context_line}</i>{phone_line}\n\n"
        f"{html_escape(message)}"
    )

    # Buttons: copy message only (Telegram rejects sms: URLs)
    buttons = [
        {"text": "📋 Copy message", "copy_text": {"text": message}},
    ]

    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={
            "chat_id": chat_id,
            "message_thread_id": thread_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": [buttons]},
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    if not resp.ok:
        print(f"  ⚠️  Telegram error: {resp.text}")
        return False
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Parse --test-date MM-DD flag
    test_date = None
    if "--test-date" in sys.argv:
        idx = sys.argv.index("--test-date")
        if idx + 1 < len(sys.argv):
            test_date = sys.argv[idx + 1]

    today     = date.today()
    mmdd      = test_date or today.strftime("%m-%d")
    now_str   = datetime.now(tz=TZ).strftime("%Y-%m-%d %H:%M %Z")

    print(f"🎂 Birthday Runner — {now_str}")
    print(f"   Checking birthdays for: {mmdd}{'  [TEST DATE]' if test_date else ''}\n")

    config    = load_config()
    api_key   = get_anthropic_key()
    bot_token = get_telegram_token()

    chat_id   = config["destination"]["chat_id"]
    thread_id = config["destination"]["thread_id"]
    min_score = config.get("min_score", 30)
    model     = config.get("model", "claude-haiku-4-5-20251001")

    # Query CRM
    print(f"🔍 Querying CRM DB (score > {min_score}, birthday = {mmdd})...")
    contacts = get_birthday_contacts(CRM_DB, mmdd, min_score)
    print(f"   Found {len(contacts)} contact(s)\n")

    if not contacts:
        print("✅ No birthdays today above score threshold — nothing to send.")
        return

    # Process each contact
    sent = 0
    for i, contact in enumerate(contacts, 1):
        name = contact.get("name", "Unknown")
        print(f"  [{i}/{len(contacts)}] {name} (score: {contact.get('score')})")
        print(f"    Generating message...")

        try:
            message = generate_birthday_message(contact, api_key, model)
            print(f"    Message: {message[:80]}...")
        except Exception as e:
            print(f"    ⚠️  AI generation failed: {e}")
            message = f"🎉 Happy Birthday {contact.get('preferred_name') or name.split()[0]}! Hope you have a great day 🎂"

        time.sleep(0.5)

        ok = send_birthday_message(contact, message, chat_id, thread_id, bot_token)
        if ok:
            sent += 1
            print(f"    ✅ Sent")
        else:
            print(f"    ❌ Failed to send")

        time.sleep(1.2)   # rate limit

    print(f"\n{'='*50}")
    print(f"🎂 Sent {sent}/{len(contacts)} birthday messages to topic {thread_id}")


if __name__ == "__main__":
    main()
