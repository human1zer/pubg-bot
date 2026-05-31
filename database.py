"""
database.py — Async SQLite storage layer for PUBG Bot

Replaces:
  - match_history.json   (match stats per player)
  - posted_matches.json  (deduplication set)

Tables
------
matches        — one row per (player, match), all tracked stats
posted_matches — set of match IDs already posted to Discord
"""

import aiosqlite
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Set

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pubg_bot.db")


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

_CREATE_MATCHES = """
CREATE TABLE IF NOT EXISTS matches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id            TEXT    NOT NULL,
    player_name         TEXT    NOT NULL,
    played_at           TEXT    NOT NULL,   -- ISO-8601 UTC
    map                 TEXT,
    game_mode           TEXT,
    match_category      TEXT,
    match_type          TEXT,
    is_custom           INTEGER DEFAULT 0,
    duration_seconds    INTEGER DEFAULT 0,

    -- player stats
    rank                INTEGER DEFAULT 99,
    kills               INTEGER DEFAULT 0,
    damage_dealt        REAL    DEFAULT 0,
    assists             INTEGER DEFAULT 0,
    dbnos               INTEGER DEFAULT 0,
    headshot_kills      INTEGER DEFAULT 0,
    longest_kill        REAL    DEFAULT 0,
    revives             INTEGER DEFAULT 0,
    revives_received    INTEGER DEFAULT 0,
    team_kills          INTEGER DEFAULT 0,
    boosts_used         INTEGER DEFAULT 0,
    heals_used          INTEGER DEFAULT 0,
    walk_distance       REAL    DEFAULT 0,
    ride_distance       REAL    DEFAULT 0,
    swim_distance       REAL    DEFAULT 0,
    survival_time_minutes REAL  DEFAULT 0,
    death_type          TEXT,
    kill_streaks        INTEGER DEFAULT 0,
    road_kills          INTEGER DEFAULT 0,
    weapons_acquired    INTEGER DEFAULT 0,

    UNIQUE(match_id, player_name)
);
"""

_CREATE_POSTED = """
CREATE TABLE IF NOT EXISTS posted_matches (
    match_id    TEXT PRIMARY KEY,
    posted_at   TEXT NOT NULL
);
"""

_CREATE_IDX_PLAYER  = "CREATE INDEX IF NOT EXISTS idx_matches_player   ON matches(player_name);"
_CREATE_IDX_PLAYED  = "CREATE INDEX IF NOT EXISTS idx_matches_played   ON matches(played_at);"
_CREATE_IDX_CATEGORY= "CREATE INDEX IF NOT EXISTS idx_matches_category ON matches(match_category);"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def init_db(path: str = DB_PATH) -> None:
    """Create tables and indexes if they don't exist."""
    async with aiosqlite.connect(path) as db:
        await db.execute(_CREATE_MATCHES)
        await db.execute(_CREATE_POSTED)
        await db.execute(_CREATE_IDX_PLAYER)
        await db.execute(_CREATE_IDX_PLAYED)
        await db.execute(_CREATE_IDX_CATEGORY)
        await db.commit()
    logger.info(f"✅ Database ready: {path}")


# ── Posted-match deduplication ───────────────────────────────────────────────

async def load_posted_matches(path: str = DB_PATH) -> Set[str]:
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute("SELECT match_id FROM posted_matches;")
        rows = await cursor.fetchall()
    ids = {row[0] for row in rows}
    logger.info(f"📋 Loaded {len(ids)} previously posted match IDs from DB")
    return ids


async def save_posted_matches(match_ids: Set[str], max_history: int = 500, path: str = DB_PATH) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(path) as db:
        await db.executemany(
            "INSERT OR IGNORE INTO posted_matches (match_id, posted_at) VALUES (?, ?);",
            [(mid, now) for mid in match_ids],
        )
        # Prune oldest entries beyond max_history
        await db.execute(
            """
            DELETE FROM posted_matches WHERE match_id NOT IN (
                SELECT match_id FROM posted_matches
                ORDER BY posted_at DESC LIMIT ?
            );
            """,
            (max_history,),
        )
        await db.commit()


# ── Match history ────────────────────────────────────────────────────────────

async def save_match_history(matches: List[dict], path: str = DB_PATH) -> None:
    """
    Insert player match records.  Each dict in `matches` is the same shape
    that bot.py builds in save_matches_for_stats():

        {
            player_name, match_id, match_category, game_mode, match_type,
            is_custom, map, duration_seconds, duration_minutes,
            played_at, played_at_formatted,
            player_stats: { rank, kills, damage_dealt, ... }
        }
    """
    rows = []
    for m in matches:
        s = m.get("player_stats", {})
        rows.append((
            m["match_id"],
            m["player_name"],
            m.get("played_at", ""),
            m.get("map", ""),
            m.get("game_mode", ""),
            m.get("match_category", ""),
            m.get("match_type", ""),
            1 if m.get("is_custom") else 0,
            m.get("duration_seconds", 0),
            s.get("rank", 99),
            s.get("kills", 0),
            round(s.get("damage_dealt", 0), 2),
            s.get("assists", 0),
            s.get("dbnos", 0),
            s.get("headshot_kills", 0),
            round(s.get("longest_kill", 0), 2),
            s.get("revives", 0),
            s.get("revives_received", 0),
            s.get("team_kills", 0),
            s.get("boosts_used", 0),
            s.get("heals_used", 0),
            round(s.get("walk_distance", 0), 2),
            round(s.get("ride_distance", 0), 2),
            round(s.get("swim_distance", 0), 2),
            round(s.get("survival_time_minutes", 0), 2),
            s.get("death_type", ""),
            s.get("kill_streaks", 0),
            s.get("road_kills", 0),
            s.get("weapons_acquired", 0),
        ))

    async with aiosqlite.connect(path) as db:
        await db.executemany(
            """
            INSERT OR IGNORE INTO matches (
                match_id, player_name, played_at, map, game_mode,
                match_category, match_type, is_custom, duration_seconds,
                rank, kills, damage_dealt, assists, dbnos, headshot_kills,
                longest_kill, revives, revives_received, team_kills,
                boosts_used, heals_used, walk_distance, ride_distance,
                swim_distance, survival_time_minutes, death_type,
                kill_streaks, road_kills, weapons_acquired
            ) VALUES (
                ?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            );
            """,
            rows,
        )
        await db.commit()
    logger.info(f"💾 Saved {len(rows)} player match records to DB")


async def get_matches_since(days: int, path: str = DB_PATH) -> List[dict]:
    """
    Return all match rows more recent than `days` ago,
    excluding CASUAL / ARCADE / AIROYALE categories.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    excluded = ("CASUAL", "ARCADE")

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM matches
            WHERE played_at >= ?
              AND UPPER(match_category) NOT IN (?, ?)
              AND UPPER(match_category) NOT LIKE '%AIROYALE%'
            ORDER BY played_at ASC;
            """,
            (cutoff, *excluded),
        )
        rows = await cursor.fetchall()

    return [dict(r) for r in rows]


async def get_all_time_best(path: str = DB_PATH) -> List[dict]:
    """
    Return the single best stat row per player across all time
    for the !best command:
      - best_kills_game   (most kills in one match)
      - best_damage_game  (most damage in one match)
      - best_rank         (lowest finish rank)
      - longest_kill      (longest kill shot ever)
    """
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT
                player_name,
                MAX(kills)          AS best_kills,
                MAX(damage_dealt)   AS best_damage,
                MIN(rank)           AS best_rank,
                MAX(longest_kill)   AS longest_kill,
                SUM(kills)          AS total_kills,
                COUNT(*)            AS total_matches,
                SUM(CASE WHEN rank = 1 THEN 1 ELSE 0 END) AS total_wins
            FROM matches
            WHERE UPPER(match_category) NOT IN ('CASUAL', 'ARCADE')
              AND UPPER(match_category) NOT LIKE '%AIROYALE%'
            GROUP BY player_name
            ORDER BY total_kills DESC;
            """
        )
        rows = await cursor.fetchall()

    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# One-time migration helpers
# ─────────────────────────────────────────────────────────────────────────────

async def migrate_json_history(json_path: str = "match_history.json", db_path: str = DB_PATH) -> None:
    """
    Import existing match_history.json rows into the SQLite DB.
    Safe to run multiple times — uses INSERT OR IGNORE.
    """
    if not os.path.exists(json_path):
        logger.info("No match_history.json found — nothing to migrate.")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        history = json.load(f)

    # Convert old format to the shape save_match_history expects
    converted = []
    for entry in history:
        converted.append({
            "match_id":       entry.get("match_id", ""),
            "player_name":    entry.get("player_name", ""),
            "played_at":      entry.get("timestamp", ""),
            "map":            entry.get("map", ""),
            "game_mode":      entry.get("mode", ""),
            "match_category": entry.get("category", ""),
            "match_type":     "",
            "is_custom":      False,
            "duration_seconds": 0,
            "player_stats":   entry.get("stats", {}),
        })

    await save_match_history(converted, path=db_path)
    logger.info(f"✅ Migrated {len(converted)} rows from {json_path} → {db_path}")


async def migrate_posted_json(json_path: str = "posted_matches.json", db_path: str = DB_PATH) -> None:
    """Import existing posted_matches.json into the SQLite DB."""
    if not os.path.exists(json_path):
        logger.info("No posted_matches.json found — nothing to migrate.")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        ids = set(json.load(f))

    await save_posted_matches(ids, path=db_path)
    logger.info(f"✅ Migrated {len(ids)} posted match IDs from {json_path} → {db_path}")
