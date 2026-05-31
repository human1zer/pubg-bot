import asyncio
import json
import logging
import os
from typing import List, Optional, Tuple

# ── .env support ─────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — fall back to config.json only

from bot import IntegratedPUBGBot
import database as db

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_players_from_file(filename: str = "players.txt") -> List[Tuple[str, str]]:
    """Load player names from file — name only, platform always defaults to steam."""
    if not os.path.exists(filename):
        logger.warning(f"⚠️ '{filename}' not found. Creating example file...")
        with open(filename, "w", encoding="utf-8") as f:
            f.write("# PUBG Players to Track\n")
            f.write("# Format: PlayerName (one per line, no platform needed)\n#\n")
            f.write("# Examples:\n# PlayerName1\n# PlayerName2\n")
        logger.info(f"✅ Created '{filename}'. Add player names and run again.")
        return []

    players = []
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name = line.split(",", 1)[0].strip() if "," in line else line
            if name:
                players.append((name, "steam"))
    return players


def load_config(filename: str = "config.json") -> Optional[dict]:
    if not os.path.exists(filename):
        logger.warning(f"⚠️ '{filename}' not found. Creating default config...")
        default_config = {
            # Secrets — prefer .env; these are fallback placeholders
            "pubg_api_key":          "YOUR_PUBG_API_KEY_HERE",
            "discord_token":         "YOUR_DISCORD_BOT_TOKEN_HERE",
            # Channel IDs
            "discord_channel_id":    123456789012345678,
            "weekly_channel_id":     123456789012345678,
            # Timing
            "check_interval_seconds": 150,
            "request_delay":          7.0,
            "max_retries":            3,
            # Optional: role ID to ping on chicken dinner (0 = disabled)
            "winner_role_id":         0,
            # How many posted match IDs to keep in the database
            "posted_matches_max_history": 500,
        }
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(default_config, f, indent=2)
        logger.info(f"✅ Created '{filename}'")

        print("\n📝 Setup Instructions:")
        print("\n=== Recommended: use .env for secrets ===")
        print("  Copy .env.example → .env and fill in PUBG_API_KEY and DISCORD_TOKEN")
        print("\n=== Or set them in config.json ===")
        print("  1. Get PUBG key from:  https://developer.pubg.com/")
        print("  2. Get Discord token:  https://discord.com/developers/applications")
        print("  3. Enable 'Message Content Intent' in the Bot settings")
        print("  4. Set discord_channel_id to the channel you want posts in")
        return None

    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 80)
    print("PUBG INTEGRATED TRACKER & DISCORD BOT v3.0")
    print("=" * 80)
    print(" ✅ Async architecture")
    print(" ✅ SQLite match history  (replaces match_history.json)")
    print(" ✅ SQLite posted-matches (replaces posted_matches.json)")
    print(" ✅ .env secret support   (PUBG_API_KEY / DISCORD_TOKEN)")
    print(" ✅ Configurable posted_matches cap")
    print(" ✅ Fixed polling loop    (tasks.loop interval)")
    print(" ✅ Chicken dinner alerts with optional role ping")
    print(" ✅ !best command — all-time personal records")
    print("=" * 80 + "\n")

    config = load_config()
    if not config:
        return

    # ── Secrets: .env wins over config.json ──────────────────────────────────
    pubg_api_key  = os.getenv("PUBG_API_KEY")  or config.get("pubg_api_key", "")
    discord_token = os.getenv("DISCORD_TOKEN") or config.get("discord_token", "")

    channel_id        = config.get("discord_channel_id")
    weekly_channel_id = config.get("weekly_channel_id", channel_id)
    check_interval    = config.get("check_interval_seconds", 150)
    request_delay     = config.get("request_delay", 7.0)
    max_retries       = config.get("max_retries", 3)
    winner_role_id    = config.get("winner_role_id", 0)
    posted_max        = config.get("posted_matches_max_history", 500)

    # ── Validate ─────────────────────────────────────────────────────────────
    if not pubg_api_key or pubg_api_key == "YOUR_PUBG_API_KEY_HERE":
        logger.error("❌ PUBG API key not set.  Add it to .env (PUBG_API_KEY) or config.json.")
        logger.info("   Get your key from: https://developer.pubg.com/")
        return

    if not discord_token or discord_token == "YOUR_DISCORD_BOT_TOKEN_HERE":
        logger.error("❌ Discord token not set.  Add it to .env (DISCORD_TOKEN) or config.json.")
        return

    if channel_id == 123456789012345678:
        logger.error("❌ Please set discord_channel_id in config.json.")
        return

    if check_interval < 60:
        logger.warning("⚠️ check_interval_seconds < 60 — you may hit PUBG API rate limits!")

    if request_delay < 6:
        logger.warning("⚠️ request_delay < 6s — you may hit PUBG API rate limits!")

    # ── Init SQLite + optional migration from old JSON files ─────────────────
    asyncio.run(db.init_db())
    asyncio.run(db.migrate_json_history())   # no-op if file doesn't exist
    asyncio.run(db.migrate_posted_json())    # no-op if file doesn't exist

    # ── Players ───────────────────────────────────────────────────────────────
    players = load_players_from_file()
    if not players:
        logger.warning("⚠️ No players found.  Add names to players.txt, or use !addplayer in Discord.")

    logger.info(f"📋 Players to track: {len(players)}")
    for idx, (name, _) in enumerate(players, 1):
        logger.info(f"  {idx}. {name}")

    logger.info(f"\n⏱️ Settings:")
    logger.info(f"  Check interval:       {check_interval}s ({check_interval / 60:.1f} min)")
    logger.info(f"  Request delay:        {request_delay}s")
    logger.info(f"  Discord channel:      {channel_id}")
    logger.info(f"  Posted-match cap:     {posted_max}")
    logger.info(f"  Winner role ping:     {'disabled' if not winner_role_id else f'<@&{winner_role_id}>'}")
    logger.info(f"\n🚀 Starting bot… (Ctrl+C to stop)\n")

    bot = IntegratedPUBGBot(
        discord_token=discord_token,
        channel_id=channel_id,
        api_key=pubg_api_key,
        players=players,
        check_interval=check_interval,
        request_delay=request_delay,
        max_retries=max_retries,
        weekly_channel_id=weekly_channel_id,
        winner_role_id=winner_role_id,
        posted_matches_max_history=posted_max,
    )

    try:
        bot.run()
    except KeyboardInterrupt:
        print("\n\n" + "=" * 80)
        print("⛔ STOPPED BY USER")
        print("=" * 80)
        print(f"Total cycles completed: {bot.cycle_number - 1}")
        print("✅ Bot stopped successfully!")


if __name__ == "__main__":
    main()
