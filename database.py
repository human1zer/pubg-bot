"""
database.py — Async SQLite storage layer for PUBG Bot

Replaces:
  - match_history.json   (match stats per player)
  - posted_matches.json  (deduplication set)

Tables
------
matches        — one row per (player, match), all tracked stats
posted_matches — set of match IDs already posted to Discord
player_cache / clan_cache / clan_kills / scanned_clan_matches
               — cross-clan rivalry tracking (see rivalry.py)
"""

import aiosqlite
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

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

_CREATE_STATE = """
CREATE TABLE IF NOT EXISTS bot_state (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

_CREATE_POSTED_SHAME = """
CREATE TABLE IF NOT EXISTS posted_shame_events (
    event_id    TEXT PRIMARY KEY,
    posted_at   TEXT NOT NULL
);
"""

_CREATE_IDX_PLAYER  = "CREATE INDEX IF NOT EXISTS idx_matches_player   ON matches(player_name);"
_CREATE_IDX_PLAYED  = "CREATE INDEX IF NOT EXISTS idx_matches_played   ON matches(played_at);"
_CREATE_IDX_CATEGORY= "CREATE INDEX IF NOT EXISTS idx_matches_category ON matches(match_category);"

# ── Cross-clan rivalry (rivalry.py) ──────────────────────────────────────────

# clan_id NULL = player has no clan (cached too, so we don't re-query them)
_CREATE_PLAYER_CACHE = """
CREATE TABLE IF NOT EXISTS player_cache (
    account_id   TEXT PRIMARY KEY,          -- "account.xxxx"
    name         TEXT NOT NULL,             -- most recent name seen
    clan_id      TEXT,                      -- "clan.xxxx" or NULL
    fetched_at   TEXT NOT NULL              -- ISO-8601 UTC, for TTL
);
"""

_CREATE_CLAN_CACHE = """
CREATE TABLE IF NOT EXISTS clan_cache (
    clan_id      TEXT PRIMARY KEY,
    clan_tag     TEXT,
    clan_name    TEXT,
    clan_level   INTEGER,
    member_count INTEGER,
    fetched_at   TEXT NOT NULL
);
"""

# One row per cross-clan kill involving a tracked player. Clan IDs are a
# snapshot at scan time, so history survives players switching clans.
_CREATE_CLAN_KILLS = """
CREATE TABLE IF NOT EXISTS clan_kills (
    match_id           TEXT NOT NULL,
    played_at          TEXT NOT NULL,
    map                TEXT,
    killer_account_id  TEXT NOT NULL,
    killer_name        TEXT NOT NULL,
    killer_clan_id     TEXT NOT NULL,
    killer_tracked     INTEGER DEFAULT 0,
    victim_account_id  TEXT NOT NULL,
    victim_name        TEXT NOT NULL,
    victim_clan_id     TEXT NOT NULL,
    victim_tracked     INTEGER DEFAULT 0,
    distance_m         REAL,
    weapon             TEXT,               -- killerDamageInfo.damageCauserName
    is_headshot        INTEGER DEFAULT 0,
    PRIMARY KEY (match_id, victim_account_id)   -- one death per player per match
);
"""

# Doubles as the rivalry work queue: rows start 'pending' and end 'done' or
# 'failed', so queued scans survive a bot restart.
_CREATE_SCANNED_CLAN = """
CREATE TABLE IF NOT EXISTS scanned_clan_matches (
    match_id       TEXT PRIMARY KEY,
    played_at      TEXT NOT NULL,
    map            TEXT,
    telemetry_url  TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',   -- pending | done | failed
    attempts       INTEGER DEFAULT 0,
    queued_at      TEXT NOT NULL,
    scanned_at     TEXT,
    kills_found    INTEGER DEFAULT 0
);
"""

_CREATE_IDX_CLAN_PAIR   = "CREATE INDEX IF NOT EXISTS idx_clan_kills_pair   ON clan_kills(killer_clan_id, victim_clan_id);"
_CREATE_IDX_CLAN_PLAYED = "CREATE INDEX IF NOT EXISTS idx_clan_kills_played ON clan_kills(played_at);"
_CREATE_IDX_CLAN_STATUS = "CREATE INDEX IF NOT EXISTS idx_scanned_clan_status ON scanned_clan_matches(status, queued_at);"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def init_db(path: str = DB_PATH) -> None:
    """Create tables and indexes if they don't exist."""
    async with aiosqlite.connect(path) as db:
        await db.execute(_CREATE_MATCHES)
        await db.execute(_CREATE_POSTED)
        await db.execute(_CREATE_STATE)
        await db.execute(_CREATE_POSTED_SHAME)
        await db.execute(_CREATE_IDX_PLAYER)
        await db.execute(_CREATE_IDX_PLAYED)
        await db.execute(_CREATE_IDX_CATEGORY)
        await db.execute(_CREATE_PLAYER_CACHE)
        await db.execute(_CREATE_CLAN_CACHE)
        await db.execute(_CREATE_CLAN_KILLS)
        await db.execute(_CREATE_SCANNED_CLAN)
        await db.execute(_CREATE_IDX_CLAN_PAIR)
        await db.execute(_CREATE_IDX_CLAN_PLAYED)
        await db.execute(_CREATE_IDX_CLAN_STATUS)
        await db.commit()
    logger.info(f"✅ Database ready: {path}")


# ── Small key/value state store (e.g. "last weekly summary posted") ─────────

async def get_state(key: str, path: str = DB_PATH) -> Optional[str]:
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute("SELECT value FROM bot_state WHERE key = ?;", (key,))
        row = await cursor.fetchone()
    return row[0] if row else None


async def set_state(key: str, value: str, path: str = DB_PATH) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            INSERT INTO bot_state (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value;
            """,
            (key, value),
        )
        await db.commit()


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


async def get_alltime_longest_kills(top_n: int = 10, path: str = DB_PATH) -> List[dict]:
    """
    Returns the top N longest kill shots ever recorded, one per player.
    Excludes CASUAL / ARCADE / AIROYALE matches.
    Pulls directly from the matches table — no JSON file needed.
    """
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                player_name,
                MAX(longest_kill) AS longest_kill,
                map,
                played_at
            FROM matches
            WHERE longest_kill > 0
              AND UPPER(match_category) NOT IN ('CASUAL', 'ARCADE')
              AND UPPER(match_category) NOT LIKE '%AIROYALE%'
            GROUP BY player_name
            ORDER BY longest_kill DESC
            LIMIT ?;
            """,
            (top_n,)
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Wall of Shame
# ─────────────────────────────────────────────────────────────────────────────

async def get_shame_stats(days: int = 7, path: str = DB_PATH) -> Dict[str, dict]:
    """
    Aggregate per-player shame stats over the last `days`, same
    CASUAL/ARCADE/AIROYALE exclusion as get_matches_since().

    Player names are folded case-insensitively (players.txt has both
    "Hasibfit" and "hasibfit") — the casing from the most recent match
    is used as the display name.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT player_name, played_at, death_type, team_kills, road_kills, kills
            FROM matches
            WHERE played_at >= ?
              AND UPPER(match_category) NOT IN ('CASUAL', 'ARCADE')
              AND UPPER(match_category) NOT LIKE '%AIROYALE%';
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()

    players: Dict[str, dict] = {}
    for r in rows:
        key = r["player_name"].lower()
        p = players.get(key)
        if p is None:
            p = players[key] = {
                "display_name": r["player_name"],
                "_latest_played_at": r["played_at"],
                "matches": 0,
                "suicides": 0,
                "team_kills": 0,
                "road_kills": 0,
                "byzone_deaths": 0,
                "logouts": 0,
                "kills": 0,
                "deaths": 0,
            }
        if r["played_at"] >= p["_latest_played_at"]:
            p["_latest_played_at"] = r["played_at"]
            p["display_name"] = r["player_name"]

        p["matches"]    += 1
        p["kills"]      += r["kills"] or 0
        p["team_kills"] += r["team_kills"] or 0
        p["road_kills"] += r["road_kills"] or 0

        death_type = r["death_type"]
        if death_type == "suicide":
            p["suicides"] += 1
        elif death_type == "byzone":
            p["byzone_deaths"] += 1
        elif death_type == "logout":
            p["logouts"] += 1
        if death_type != "alive":
            p["deaths"] += 1

    return players


async def get_shame_candidate_rows(days: int = 3, path: str = DB_PATH) -> List[dict]:
    """
    Raw match rows from the last `days` that are shame-worthy: a suicide,
    a blue-zone death, a logout, a teamkill, or a roadkill. Feeds the
    weekly digest lines posted alongside the Wall of Shame board. Same
    CASUAL/ARCADE/AIROYALE exclusion.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, player_name, played_at, death_type, team_kills, road_kills
            FROM matches
            WHERE played_at >= ?
              AND UPPER(match_category) NOT IN ('CASUAL', 'ARCADE')
              AND UPPER(match_category) NOT LIKE '%AIROYALE%'
              AND (
                    death_type IN ('suicide', 'byzone', 'logout')
                 OR team_kills > 0
                 OR road_kills > 0
              )
            ORDER BY played_at DESC;
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ── Shame-digest deduplication (mirrors posted_matches) ──────────────────────

async def load_posted_shame_events(path: str = DB_PATH) -> Set[str]:
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute("SELECT event_id FROM posted_shame_events;")
        rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def save_posted_shame_events(event_ids: Set[str], max_history: int = 2000, path: str = DB_PATH) -> None:
    if not event_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(path) as db:
        await db.executemany(
            "INSERT OR IGNORE INTO posted_shame_events (event_id, posted_at) VALUES (?, ?);",
            [(eid, now) for eid in event_ids],
        )
        await db.execute(
            """
            DELETE FROM posted_shame_events WHERE event_id NOT IN (
                SELECT event_id FROM posted_shame_events
                ORDER BY posted_at DESC LIMIT ?
            );
            """,
            (max_history,),
        )
        await db.commit()


# ── Cross-clan rivalry: scan queue ───────────────────────────────────────────

async def enqueue_clan_scan(
    match_id: str, played_at: str, map_name: str, telemetry_url: str, path: str = DB_PATH
) -> bool:
    """Queue a match for rivalry scanning. Returns False if already known."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO scanned_clan_matches
                (match_id, played_at, map, telemetry_url, queued_at)
            VALUES (?, ?, ?, ?, ?);
            """,
            (match_id, played_at, map_name, telemetry_url, now),
        )
        await db.commit()
    return cursor.rowcount > 0


async def next_pending_clan_scan(path: str = DB_PATH) -> Optional[dict]:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT match_id, played_at, map, telemetry_url, attempts
            FROM scanned_clan_matches
            WHERE status = 'pending'
            ORDER BY queued_at
            LIMIT 1;
            """
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def mark_clan_scan_done(match_id: str, kills_found: int, path: str = DB_PATH) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            UPDATE scanned_clan_matches
            SET status = 'done', scanned_at = ?, kills_found = ?
            WHERE match_id = ?;
            """,
            (now, kills_found, match_id),
        )
        await db.commit()


async def mark_clan_scan_failed_attempt(match_id: str, max_attempts: int, path: str = DB_PATH) -> None:
    """Bump the attempt counter; give up ('failed') after max_attempts."""
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            UPDATE scanned_clan_matches
            SET attempts = attempts + 1,
                status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'pending' END
            WHERE match_id = ?;
            """,
            (max_attempts, match_id),
        )
        await db.commit()


# ── Cross-clan rivalry: player / clan cache ──────────────────────────────────

async def get_cached_players(
    account_ids: List[str], max_age_days: int, path: str = DB_PATH
) -> Dict[str, Optional[str]]:
    """account_id -> clan_id (or None) for cache entries younger than max_age_days."""
    if not account_ids:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    placeholders = ",".join("?" * len(account_ids))
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            f"""
            SELECT account_id, clan_id FROM player_cache
            WHERE account_id IN ({placeholders}) AND fetched_at >= ?;
            """,
            (*account_ids, cutoff),
        )
        rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}


async def upsert_cached_players(players: List[dict], path: str = DB_PATH) -> None:
    """players: [{account_id, name, clan_id}]"""
    if not players:
        return
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(path) as db:
        await db.executemany(
            """
            INSERT INTO player_cache (account_id, name, clan_id, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                name = excluded.name,
                clan_id = excluded.clan_id,
                fetched_at = excluded.fetched_at;
            """,
            [(p["account_id"], p["name"], p["clan_id"], now) for p in players],
        )
        await db.commit()


async def get_fresh_clan_ids(clan_ids: List[str], max_age_days: int, path: str = DB_PATH) -> Set[str]:
    if not clan_ids:
        return set()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    placeholders = ",".join("?" * len(clan_ids))
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            f"SELECT clan_id FROM clan_cache WHERE clan_id IN ({placeholders}) AND fetched_at >= ?;",
            (*clan_ids, cutoff),
        )
        rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def get_clan_tags(clan_ids: List[str], path: str = DB_PATH) -> Dict[str, str]:
    """clan_id -> clan_tag for whatever is cached (any age)."""
    if not clan_ids:
        return {}
    placeholders = ",".join("?" * len(clan_ids))
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            f"SELECT clan_id, clan_tag FROM clan_cache WHERE clan_id IN ({placeholders});",
            tuple(clan_ids),
        )
        rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows if row[1]}


async def upsert_cached_clan(clan: dict, path: str = DB_PATH) -> None:
    """clan: {clan_id, clan_tag, clan_name, clan_level, member_count}"""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            INSERT INTO clan_cache (clan_id, clan_tag, clan_name, clan_level, member_count, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(clan_id) DO UPDATE SET
                clan_tag = excluded.clan_tag,
                clan_name = excluded.clan_name,
                clan_level = excluded.clan_level,
                member_count = excluded.member_count,
                fetched_at = excluded.fetched_at;
            """,
            (clan["clan_id"], clan["clan_tag"], clan["clan_name"],
             clan["clan_level"], clan["member_count"], now),
        )
        await db.commit()


# ── Cross-clan rivalry: kills + leaderboard ──────────────────────────────────

async def save_clan_kills(kills: List[dict], path: str = DB_PATH) -> None:
    if not kills:
        return
    async with aiosqlite.connect(path) as db:
        await db.executemany(
            """
            INSERT OR IGNORE INTO clan_kills (
                match_id, played_at, map,
                killer_account_id, killer_name, killer_clan_id, killer_tracked,
                victim_account_id, victim_name, victim_clan_id, victim_tracked,
                distance_m, weapon, is_headshot
            ) VALUES (
                :match_id, :played_at, :map,
                :killer_account_id, :killer_name, :killer_clan_id, :killer_tracked,
                :victim_account_id, :victim_name, :victim_clan_id, :victim_tracked,
                :distance_m, :weapon, :is_headshot
            );
            """,
            kills,
        )
        await db.commit()


async def get_clan_rivalries(days: Optional[int] = None, limit: int = 10, path: str = DB_PATH) -> List[dict]:
    """
    Opponent clans ranked by total encounters with tracked players:
    kills = tracked players killing that clan, deaths = that clan killing
    tracked players. Kills between two tracked players are left out.
    """
    cutoff = (
        (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else ""
    )
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT r.clan_id, c.clan_tag, c.clan_name,
                   SUM(r.kill) AS kills, SUM(r.death) AS deaths
            FROM (
                SELECT victim_clan_id AS clan_id, 1 AS kill, 0 AS death
                FROM clan_kills
                WHERE killer_tracked = 1 AND victim_tracked = 0 AND played_at >= ?
                UNION ALL
                SELECT killer_clan_id, 0, 1
                FROM clan_kills
                WHERE victim_tracked = 1 AND killer_tracked = 0 AND played_at >= ?
            ) AS r
            LEFT JOIN clan_cache c ON c.clan_id = r.clan_id
            GROUP BY r.clan_id
            ORDER BY kills + deaths DESC, kills DESC
            LIMIT ?;
            """,
            (cutoff, cutoff, limit),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]
