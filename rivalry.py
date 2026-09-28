"""
rivalry.py — Cross-clan rivalry tracking.

For every NORMAL/RANKED match the tracker picks up, downloads the match
telemetry and records each LogPlayerKillV2 where a tracked player is the
killer or the victim and the two sides are in different PUBG clans.

Clan lookups only cover the accounts involved in those kills — never the
whole lobby — and go through player_cache / clan_cache (7-day TTL by
default), batching uncached accounts 10 at a time via
/players?filter[playerIds]=...

Rate limit
----------
/players and /clans share the tracker's API key and per-minute budget, so
the scanner is throttled to never starve the main tracker:
  * work is queued in SQLite (scanned_clan_matches) and drained by a single
    background worker, one match at a time
  * the worker only makes rate-limited calls while the tracker is idle
    between cycles (tracker_busy() / tracker_idle())
  * it leaves `reserve` requests untouched in the current rate-limit window
    and sleeps until the window resets rather than dipping into them
  * calls are spaced by the tracker's request_delay
Telemetry downloads (CDN) aren't rate limited and skip the gate.
"""

import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional

import aiohttp

import database as db
from tracker import AsyncPUBGMatchTracker

logger = logging.getLogger(__name__)

SCANNED_CATEGORIES = {"NORMAL", "RANKED"}   # arcade/TDM have respawns + noise
PLAYER_BATCH_SIZE  = 10                     # PUBG API max for filter[playerIds]
MAX_ATTEMPTS       = 3
IDLE_POLL_SECONDS  = 300
RETRY_BACKOFF      = 60
TELEMETRY_TIMEOUT  = aiohttp.ClientTimeout(total=120)


class ScanError(Exception):
    """A scan step failed in a way worth retrying later."""


@dataclass
class ScanStats:
    """Breakdown of one scan, for debugging (scripts/clan_kill_scanner.py)."""
    kill_events: int = 0        # all LogPlayerKillV2 events
    tracked_events: int = 0     # ... where killer or victim is tracked
    recorded: int = 0
    skipped: Counter = field(default_factory=Counter)   # reason -> count (tracked events only)
    # (killer_name, killer_clan_id, victim_name, victim_clan_id, outcome) per tracked event
    events: list = field(default_factory=list)


class RivalryScanner:
    def __init__(
        self,
        tracker: AsyncPUBGMatchTracker,
        tracked_names: Callable[[], Iterable[str]],
        platform: str = "steam",
        reserve: int = 4,
        cache_days: int = 7,
    ):
        self.tracker       = tracker
        self.tracked_names = tracked_names
        self.base          = f"{tracker.base_url}/{platform}"
        self.reserve       = reserve
        self.cache_days    = cache_days

        self._idle = asyncio.Event()
        self._idle.set()
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    # ─────────────────────────────────────────────────────────────────────────
    # Lifecycle / hooks for the match-check loop
    # ─────────────────────────────────────────────────────────────────────────

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="rivalry-scanner")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def tracker_busy(self):
        self._idle.clear()

    def tracker_idle(self):
        self._idle.set()

    async def enqueue(self, match: dict):
        if match.get("match_category") not in SCANNED_CATEGORIES:
            return
        if not match.get("telemetry_url"):
            logger.warning(f"⚔️ No telemetry URL for match {match['match_id'][:16]}… — not queued")
            return
        queued = await db.enqueue_clan_scan(
            match["match_id"], match["played_at"], match.get("map"), match["telemetry_url"]
        )
        if queued:
            logger.info(f"⚔️ Queued rivalry scan: {match['match_id'][:16]}…")
            self._wake.set()

    # ─────────────────────────────────────────────────────────────────────────
    # Worker
    # ─────────────────────────────────────────────────────────────────────────

    async def _run(self):
        logger.info(f"⚔️ Rivalry scanner running (reserve {self.reserve} req, cache {self.cache_days}d)")
        while True:
            try:
                row = await db.next_pending_clan_scan()
                if not row:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=IDLE_POLL_SECONDS)
                    except asyncio.TimeoutError:
                        pass
                    continue

                try:
                    kills = await self.scan_match(row)
                except ScanError as e:
                    logger.warning(
                        f"⚔️ Scan failed for {row['match_id'][:16]}… "
                        f"(attempt {row['attempts'] + 1}/{MAX_ATTEMPTS}): {e}"
                    )
                    await db.mark_clan_scan_failed_attempt(row["match_id"], MAX_ATTEMPTS)
                    await asyncio.sleep(RETRY_BACKOFF)
                    continue

                await db.mark_clan_scan_done(row["match_id"], len(kills))
                logger.info(f"⚔️ Scanned {row['match_id'][:16]}… — {len(kills)} cross-clan kill(s)")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"❌ Rivalry scanner error: {e}")
                await asyncio.sleep(RETRY_BACKOFF)

    async def _acquire(self):
        """Block until a rate-limited call can be made without eating into
        the tracker's share of the budget."""
        await asyncio.sleep(self.tracker.request_delay)
        while True:
            await self._idle.wait()
            remaining = self.tracker.rate_remaining
            reset     = self.tracker.rate_reset
            now       = time.time()
            if remaining is None or remaining > self.reserve or (reset and now >= reset):
                return
            wait = (reset - now + 1) if reset else 60
            logger.info(f"⚔️ Rate budget low ({remaining} left) — rivalry scanner waiting {wait:.0f}s")
            await asyncio.sleep(wait)

    # ─────────────────────────────────────────────────────────────────────────
    # Scanning
    # ─────────────────────────────────────────────────────────────────────────

    async def scan_match(
        self, row: dict, save: bool = True, stats: Optional[ScanStats] = None
    ) -> List[dict]:
        """Scan one match; returns the cross-clan kills found (and saves them
        to clan_kills unless save=False). Pass a ScanStats to get a
        breakdown of what was skipped and why."""
        stats     = stats if stats is not None else ScanStats()
        telemetry = await self._fetch_telemetry(row["telemetry_url"])
        tracked   = {n.lower() for n in self.tracked_names()}

        def skip(killer, victim, reason, k_clan=None, v_clan=None):
            stats.skipped[reason] += 1
            stats.events.append((killer.get("name", ""), k_clan, victim.get("name", ""), v_clan, reason))

        candidates = []
        for event in telemetry:
            if event.get("_T") != "LogPlayerKillV2":
                continue
            stats.kill_events += 1
            killer = event.get("killer") or {}
            victim = event.get("victim") or {}
            k_tracked = killer.get("name", "").lower() in tracked
            v_tracked = victim.get("name", "").lower() in tracked
            if not (k_tracked or v_tracked):
                continue
            stats.tracked_events += 1

            k_id, v_id = killer.get("accountId", ""), victim.get("accountId", "")
            if event.get("isSuicide") or (k_id and k_id == v_id):
                skip(killer, victim, "suicide")
            elif not k_id:
                skip(killer, victim, "environment (no killer)")
            elif not (k_id.startswith("account.") and v_id.startswith("account.")):
                skip(killer, victim, "bot")
            else:
                candidates.append((event, killer, victim, k_tracked, v_tracked))

        if not candidates:
            return []

        names = {}
        for _, killer, victim, _, _ in candidates:
            names[killer["accountId"]] = killer.get("name", "")
            names[victim["accountId"]] = victim.get("name", "")
        clans = await self._resolve_player_clans(names)
        await self._resolve_clans({c for c in clans.values() if c})

        kills = []
        for event, killer, victim, k_tracked, v_tracked in candidates:
            k_clan = clans.get(killer["accountId"])
            v_clan = clans.get(victim["accountId"])
            if not k_clan or not v_clan:
                skip(killer, victim, "no clan", k_clan, v_clan)
                continue
            if k_clan == v_clan:
                skip(killer, victim, "same clan", k_clan, v_clan)
                continue
            stats.events.append((killer.get("name", ""), k_clan, victim.get("name", ""), v_clan, "recorded"))
            dmg = event.get("killerDamageInfo") or {}
            kills.append({
                "match_id":          row["match_id"],
                "played_at":         row["played_at"],
                "map":               row["map"],
                "killer_account_id": killer["accountId"],
                "killer_name":       killer.get("name", ""),
                "killer_clan_id":    k_clan,
                "killer_tracked":    int(k_tracked),
                "victim_account_id": victim["accountId"],
                "victim_name":       victim.get("name", ""),
                "victim_clan_id":    v_clan,
                "victim_tracked":    int(v_tracked),
                "distance_m":        round((dmg.get("distance") or 0) / 100, 1),
                "weapon":            dmg.get("damageCauserName"),
                "is_headshot":       int(dmg.get("damageReason") == "HeadShot"),
            })

        stats.recorded = len(kills)
        if save:
            await db.save_clan_kills(kills)
        return kills

    async def _fetch_telemetry(self, url: str) -> list:
        await self.tracker.ensure_session()
        try:
            # No auth header — telemetry lives on a public CDN, outside the rate limit
            async with self.tracker.session.get(url, timeout=TELEMETRY_TIMEOUT) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise ScanError(f"telemetry download failed: {e}") from e

    async def _resolve_player_clans(self, names: Dict[str, str]) -> Dict[str, Optional[str]]:
        """account_id -> clan_id (None = no clan), hitting the API only for
        accounts missing from / stale in player_cache."""
        ids    = list(names)
        clans  = await db.get_cached_players(ids, self.cache_days)
        missing = [i for i in ids if i not in clans]

        for start in range(0, len(missing), PLAYER_BATCH_SIZE):
            batch = missing[start:start + PLAYER_BATCH_SIZE]
            await self._acquire()
            data = await self.tracker.make_request_with_retry(
                f"{self.base}/players?filter[playerIds]={','.join(batch)}",
                f"rivalry players batch ({len(batch)})",
            )
            if data is None:
                raise ScanError("player batch lookup failed")

            found = {p["id"]: p["attributes"] for p in data.get("data", [])}
            rows = []
            for account_id in batch:
                attrs = found.get(account_id, {})
                # Accounts the API doesn't return are cached as clanless so we
                # don't keep asking for them.
                clan_id = attrs.get("clanId") or None
                clans[account_id] = clan_id
                rows.append({
                    "account_id": account_id,
                    "name":       attrs.get("name") or names[account_id],
                    "clan_id":    clan_id,
                })
            await db.upsert_cached_players(rows)

        return clans

    async def _resolve_clans(self, clan_ids: set):
        """Fill clan_cache for display. Failures are non-fatal: kills only
        need the clan ID, and the tag is retried on the next scan."""
        fresh = await db.get_fresh_clan_ids(list(clan_ids), self.cache_days)
        for clan_id in clan_ids - fresh:
            await self._acquire()
            data = await self.tracker.make_request_with_retry(
                f"{self.base}/clans/{clan_id}", f"rivalry clan {clan_id[:16]}"
            )
            if not data:
                continue
            attrs = data["data"]["attributes"]
            await db.upsert_cached_clan({
                "clan_id":      clan_id,
                "clan_tag":     attrs.get("clanTag"),
                "clan_name":    attrs.get("clanName"),
                "clan_level":   attrs.get("clanLevel"),
                "member_count": attrs.get("clanMemberCount"),
            })
