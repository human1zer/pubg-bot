"""
config.py — Shared config/secret loader for all entry points.

Precedence: .env / real environment variables win over config.json.
config.json only holds non-secret settings plus fallback placeholders.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — fall back to config.json only

DEFAULT_CONFIG = {
    # Secrets — prefer .env; these are fallback placeholders
    "pubg_api_key":          "YOUR_PUBG_API_KEY_HERE",
    "discord_token":         "YOUR_DISCORD_BOT_TOKEN_HERE",
    # PUBG channel IDs
    "discord_channel_id":    123456789012345678,
    "weekly_channel_id":     123456789012345678,
    "winner_channel_id":     123456789012345678,
    # When true, !shametest previews the weekly shame post (embed + digest
    # lines) by logging it to stdout and echoing it back to the invoking
    # channel, instead of posting it to the real weekly channel.
    "shame_dry_run":           False,
    # How many players to list per Wall of Shame award (worst first).
    "shame_top_n":             5,
    # Shared schedule for the weekly summary + Wall of Shame run, posted in
    # that order in the same run, both to weekly_channel_id. Day follows
    # Python's datetime.weekday(): Monday=0 … Sunday=6. Timezone is an IANA
    # zone name — a local zone (rather than UTC) keeps the wall-clock hour
    # fixed across daylight saving changes.
    "weekly_post_day":         6,
    "weekly_post_hour":        18,
    "weekly_post_timezone":    "Europe/Oslo",
    # PUBG timing
    "check_interval_seconds": 150,
    "request_delay":          9.0,
    "max_retries":            2,
    # Cross-clan rivalry tracking (rivalry.py). The scanner shares the PUBG
    # API budget with the tracker: it only runs between tracker cycles and
    # always leaves rivalry_reserve_requests unused in the current rate-limit
    # window. Player/clan lookups are cached for rivalry_cache_days.
    "rivalry_enabled":          True,
    "rivalry_reserve_requests": 4,
    "rivalry_cache_days":       7,
    # Optional: role ID to ping on chicken dinner (0 = disabled)
    "winner_role_id":         0,
    # How many posted match IDs to keep in the database
    "posted_matches_max_history": 500,
    # Birthday bot
    "birthday_channel_id":         123456789012345678,
    "birthday_announce_hour_utc":  8,
    "birthday_role_name":          "🎂 Birthday",
    "pubg_channel_id":             0,
    # !ask — local Ollama chat. keep_alive controls how long the model stays
    # loaded in VRAM after a question (so the GPU frees up afterwards).
    "ask_ollama_url":    "http://localhost:11434/api/chat",
    "ask_model":         "llama3.2:3b",
    "ask_keep_alive":    "5m",
    "ask_system_prompt": "You are a sarcastic Discord bot in a PUBG clan server. Always answer with dry irony and mockery, max 2 sentences. Playful roasting, never hateful. Reply in the same language as the question.",
}


def load_config(filename: str = CONFIG_PATH) -> dict | None:
    """Load config.json, creating a default file on first run."""
    if not os.path.exists(filename):
        logger.warning(f"⚠️ '{filename}' not found. Creating default config...")
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        logger.info(f"✅ Created '{filename}'")

        print("\n📝 Setup Instructions:")
        print("\n=== Recommended: use .env for secrets ===")
        print("  Copy .env.example → .env and fill in PUBG_API_KEY and DISCORD_TOKEN")
        print("\n=== Or set them in config.json ===")
        print("  1. Get PUBG key from:  https://developer.pubg.com/")
        print("  2. Get Discord token:  https://discord.com/developers/applications")
        print("  3. Enable 'Message Content Intent' in the Bot settings")
        print("  4. Set discord_channel_id / birthday_channel_id to real channels")
        return None

    with open(filename, "r", encoding="utf-8") as f:
        config = json.load(f)

    # ── Secrets: .env wins over config.json ──────────────────────────────────
    config["pubg_api_key"]  = os.getenv("PUBG_API_KEY")  or config.get("pubg_api_key", "")
    config["discord_token"] = os.getenv("DISCORD_TOKEN") or config.get("discord_token", "")
    return config
