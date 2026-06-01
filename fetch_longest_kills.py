"""
fetch_longest_kills.py — Replaces scrape_longest_kills.py

Fetches all-time longest kill per player directly from the PUBG Lifetime Stats API
and stores results in the SQLite database (pubg_bot.db).

Runs as a systemd timer every Wednesday at 15:00 (before the weekly summary at 18:00).
No more Discord scraping. No more JSON files.
"""

import asyncio
import aiohttp
import json
import logging
import os
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
PLAYERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "players.txt")
DB_PATH      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pubg_bot.db")
REQUEST_DELAY = 10.0

# ── Config ────────────────────────────────────────────────────────────────────

with open(CONFIG_PATH) as f:
    config = json.load(f)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_KEY = os.getenv("PUBG_API_KEY") or config.get("pubg_api_key")
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/vnd.api+json",
}

# ── Load players ──────────────────────────────────────────────────────────────

def load_players():
    players = []
    with open(PLAYERS_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                players.append(line.split(",")[0].strip())
    return players

# ── API ───────────────────────────────────────────────────────────────────────

async def get_player_id(session, player_name):
    url = f"https://api.pubg.com/shards/steam/players?filter[playerNames]={player_name}"
    async with session.get(url, headers=HEADERS) as resp:
        if resp.status != 200:
            logger.warning(f"  ❌ Could not find player: {player_name} (status {resp.status})")
            return None
        data = await resp.json()
        players = data.get("data", [])
        if not players:
            return None
        return players[0]["id"]


async def get_lifetime_longest_kill(session, player_id, player_name):
    url = f"https://api.pubg.com/shards/steam/players/{player_id}/seasons/lifetime"
    async with session.get(url, headers=HEADERS) as resp:
        if resp.status != 200:
            logger.warning(f"  ❌ Lifetime stats failed for {player_name} (status {resp.status})")
            return None
        data = await resp.json()
        attrs = data.get("data", {}).get("attributes", {})
        game_mode_stats = attrs.get("gameModeStats", {})

        best_longest = 0
        best_mode    = ""
        for mode in ["squad", "squad-fpp", "duo", "duo-fpp", "solo", "solo-fpp"]:
            stats = game_mode_stats.get(mode, {})
            lk = stats.get("longestKill", 0)
            if lk > best_longest:
                best_longest = lk
                best_mode    = mode

        return {"longest_kill": round(best_longest, 2), "mode": best_mode}

# ── Database ──────────────────────────────────────────────────────────────────

async def save_to_db(results):
    """
    Store lifetime longest kills in a dedicated table.
    Creates the table if it doesn't exist.
    """
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS lifetime_longest_kills (
                player_name  TEXT PRIMARY KEY,
                longest_kill REAL NOT NULL,
                game_mode    TEXT,
                updated_at   TEXT NOT NULL
            );
        """)
        now = datetime.now(timezone.utc).isoformat()
        await db.executemany(
            """
            INSERT INTO lifetime_longest_kills (player_name, longest_kill, game_mode, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(player_name) DO UPDATE SET
                longest_kill=excluded.longest_kill,
                game_mode=excluded.game_mode,
                updated_at=excluded.updated_at;
            """,
            [(name, lk, mode, now) for name, lk, mode in results if lk]
        )
        await db.commit()
    logger.info(f"✅ Saved {len([r for r in results if r[1]])} records to DB")


async def get_from_db():
    """Read lifetime longest kills from DB — used by weekly_stats.py."""
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT player_name, longest_kill, game_mode, updated_at
            FROM lifetime_longest_kills
            ORDER BY longest_kill DESC;
        """)
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    players = load_players()
    logger.info(f"📋 {len(players)} players to fetch")
    logger.info(f"⏱️  Delay: {REQUEST_DELAY}s per player\n")

    results = []

    async with aiohttp.ClientSession() as session:
        for idx, player_name in enumerate(players, 1):
            logger.info(f"[{idx}/{len(players)}] {player_name}")

            player_id = await get_player_id(session, player_name)
            if not player_id:
                results.append((player_name, None, None))
                await asyncio.sleep(REQUEST_DELAY)
                continue

            await asyncio.sleep(6)

            stats = await get_lifetime_longest_kill(session, player_id, player_name)
            if stats:
                lk   = stats["longest_kill"]
                mode = stats["mode"]
                logger.info(f"  ✅ {lk}m [{mode}]")
                results.append((player_name, lk, mode))
            else:
                results.append((player_name, None, None))

            if idx < len(players):
                await asyncio.sleep(REQUEST_DELAY)

    await save_to_db(results)

    # Print summary
    valid = [(n, lk, m) for n, lk, m in results if lk]
    failed = [n for n, lk, m in results if not lk]
    valid.sort(key=lambda x: x[1], reverse=True)

    logger.info("\n" + "="*60)
    logger.info("RESULTS")
    logger.info("="*60)
    medals = ["🥇", "🥈", "🥉"]
    for i, (name, lk, mode) in enumerate(valid):
        medal = medals[i] if i < 3 else f"{i+1}."
        logger.info(f"{medal} {name}: {lk}m [{mode}]")
    if failed:
        logger.warning(f"\n⚠️  No data for: {', '.join(failed)}")
    logger.info("="*60)


if __name__ == "__main__":
    asyncio.run(main())