import asyncio
import logging
import os
from typing import List, Tuple

import discord
from discord.ext import commands

import database as db
from config import load_config
from bot import setup as setup_pubg_cog
from birthday_bot import setup as setup_birthday_cog
from song_bot import setup as setup_song_cog

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


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def run_bot(config: dict, players: List[Tuple[str, str]]) -> None:
    channel_id         = config.get("discord_channel_id")
    weekly_channel_id  = config.get("weekly_channel_id", channel_id)
    winner_channel_id  = config.get("winner_channel_id", channel_id)
    check_interval     = config.get("check_interval_seconds", 150)
    request_delay      = config.get("request_delay", 7.0)
    max_retries        = config.get("max_retries", 3)
    winner_role_id     = config.get("winner_role_id", 0)
    posted_max         = config.get("posted_matches_max_history", 500)
    shame_dry_run       = config.get("shame_dry_run", False)
    shame_top_n          = config.get("shame_top_n", 5)
    weekly_post_day      = config.get("weekly_post_day", 6)
    weekly_post_hour     = config.get("weekly_post_hour", 18)
    weekly_post_timezone = config.get("weekly_post_timezone", "Europe/Oslo")

    birthday_channel_id = config.get("birthday_channel_id", 0)
    birthday_role_name  = config.get("birthday_role_name", "🎂 Birthday")
    announce_hour_utc   = config.get("birthday_announce_hour_utc", 8)
    pubg_channel_id     = config.get("pubg_channel_id", 0)

    song_channel_id     = config.get("song_channel_id", 0)
    song_post_day       = config.get("song_post_day", 4)
    song_post_hour      = config.get("song_post_hour", 20)

    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    async with bot:
        await setup_pubg_cog(
            bot,
            channel_id=channel_id,
            api_key=config["pubg_api_key"],
            players=players,
            check_interval=check_interval,
            request_delay=request_delay,
            max_retries=max_retries,
            weekly_channel_id=weekly_channel_id,
            winner_role_id=winner_role_id,
            winner_channel_id=winner_channel_id,
            posted_matches_max_history=posted_max,
            shame_dry_run=shame_dry_run,
            shame_top_n=shame_top_n,
            weekly_post_day=weekly_post_day,
            weekly_post_hour=weekly_post_hour,
            weekly_post_timezone=weekly_post_timezone,
        )
        if birthday_channel_id:
            await setup_birthday_cog(
                bot,
                birthday_channel_id=birthday_channel_id,
                birthday_role_name=birthday_role_name,
                announce_hour_utc=announce_hour_utc,
                pubg_channel_id=pubg_channel_id,
            )
        else:
            logger.warning("⚠️ birthday_channel_id not set — birthday bot disabled.")

        if song_channel_id:
            await setup_song_cog(
                bot,
                channel_id=song_channel_id,
                post_day=song_post_day,
                post_hour=song_post_hour,
                timezone=weekly_post_timezone,
            )
        else:
            logger.warning("⚠️ song_channel_id not set — Song Check disabled.")

        await bot.start(config["discord_token"])


def main():
    print("=" * 80)
    print("PUBG + BIRTHDAY DISCORD BOT")
    print("=" * 80)
    print(" ✅ Single bot process — PUBG + Birthday cogs share one client")
    print(" ✅ SQLite match history & posted-matches")
    print(" ✅ .env secret support   (PUBG_API_KEY / DISCORD_TOKEN)")
    print(" ✅ Restart-safe weekly summary posting")
    print(" ✅ Chicken dinner alerts with optional role ping")
    print(" ✅ Birthday × PUBG crossover (chicken dinner on your birthday)")
    print("=" * 80 + "\n")

    config = load_config()
    if not config:
        return

    channel_id = config.get("discord_channel_id")

    # ── Validate ─────────────────────────────────────────────────────────────
    if not config["pubg_api_key"] or config["pubg_api_key"] == "YOUR_PUBG_API_KEY_HERE":
        logger.error("❌ PUBG API key not set.  Add it to .env (PUBG_API_KEY) or config.json.")
        logger.info("   Get your key from: https://developer.pubg.com/")
        return

    if not config["discord_token"] or config["discord_token"] == "YOUR_DISCORD_BOT_TOKEN_HERE":
        logger.error("❌ Discord token not set.  Add it to .env (DISCORD_TOKEN) or config.json.")
        return

    if channel_id == 123456789012345678:
        logger.error("❌ Please set discord_channel_id in config.json.")
        return

    check_interval = config.get("check_interval_seconds", 150)
    request_delay  = config.get("request_delay", 7.0)
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
    logger.info(f"\n🚀 Starting bot… (Ctrl+C to stop)\n")

    try:
        asyncio.run(run_bot(config, players))
    except KeyboardInterrupt:
        print("\n\n" + "=" * 80)
        print("⛔ STOPPED BY USER")
        print("=" * 80)
        print("✅ Bot stopped successfully!")


if __name__ == "__main__":
    main()
