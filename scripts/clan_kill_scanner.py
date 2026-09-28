"""
clan_kill_scanner.py — dry-run rivalry scan of one player's latest match.

Runs the same RivalryScanner the bot uses (rivalry.py) on demand and
prints what it would record, plus a breakdown of every kill involving a
tracked player (players.txt + the given name) and why it was skipped.

Scan only: it does not queue the match, mark it scanned, or save kills,
so the bot still scans the match normally. The only DB writes are the
player/clan lookup caches (player_cache / clan_cache).

Usage:
    python scripts/clan_kill_scanner.py [player_name]           # player's latest match
    python scripts/clan_kill_scanner.py --match <match_id>      # a specific match

Requires PUBG_API_KEY in .env (see .env.example) or config.json — same
credentials as the rest of the bot.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db                   # noqa: E402
from config import load_config          # noqa: E402
from Main import load_players_from_file  # noqa: E402
from rivalry import RivalryScanner, ScanStats  # noqa: E402
from tracker import AsyncPUBGMatchTracker  # noqa: E402

PLATFORM = "steam"


async def main(player_name: str, match_id: str, api_key: str, request_delay: float):
    await db.init_db()
    tracker = AsyncPUBGMatchTracker(api_key, request_delay=request_delay)
    tracked = [name for name, _ in load_players_from_file()]
    if player_name and player_name.lower() not in {n.lower() for n in tracked}:
        tracked.append(player_name)
    scanner = RivalryScanner(tracker, lambda: tracked, platform=PLATFORM)

    try:
        if match_id:
            print(f"[1] Fetching match {match_id}...")
            match = await tracker.get_match_details(match_id, PLATFORM, tracked)
        else:
            print(f"[1] Fetching latest match for {player_name}...")
            match = await tracker.get_latest_match(player_name, PLATFORM, tracked)
        if not match:
            print("❌ Could not fetch a match.")
            return
        if not match.get("telemetry_url"):
            print("❌ Match has no telemetry URL.")
            return
        print(f"  Match: {match['match_id']} | {match['map']} | {match['match_category']}")

        print("\n[2] Scanning telemetry (dry run — nothing saved)...")
        stats = ScanStats()
        kills = await scanner.scan_match({
            "match_id":      match["match_id"],
            "played_at":     match["played_at"],
            "map":           match["map"],
            "telemetry_url": match["telemetry_url"],
        }, save=False, stats=stats)
    finally:
        await tracker.close_session()

    tags = await db.get_clan_tags(list({c for e in stats.events for c in (e[1], e[3]) if c}))

    def who(name, clan_id):
        return f"[{tags.get(clan_id, clan_id[:13])}]{name}" if clan_id else f"{name} (no clan)"

    print(f"\n{'=' * 60}")
    print("DEBUG SUMMARY")
    print(f"  LogPlayerKillV2 events:      {stats.kill_events}")
    print(f"  involving tracked players:   {stats.tracked_events}")
    print(f"  recorded (cross-clan):       {stats.recorded}")
    print(f"  skipped:                     {sum(stats.skipped.values())}")
    for reason in ("suicide", "bot", "environment (no killer)", "no clan", "same clan"):
        print(f"    {reason:26s} {stats.skipped.get(reason, 0)}")

    if stats.events:
        print("\nTRACKED KILL EVENTS:")
        for k_name, k_clan, v_name, v_clan, outcome in stats.events:
            # Clans are only resolved for events that got past the bot/suicide checks
            resolved = outcome in ("no clan", "same clan", "recorded")
            killer = who(k_name, k_clan) if resolved else (k_name or "—")
            victim = who(v_name, v_clan) if resolved else v_name
            print(f"  {killer} → {victim}  [{outcome}]")
    print("=" * 60)


if __name__ == "__main__":
    config = load_config()
    if not config:
        sys.exit(1)
    api_key = config.get("pubg_api_key")
    if not api_key or api_key == "YOUR_PUBG_API_KEY_HERE":
        print("❌ PUBG API key not set. Add it to .env (PUBG_API_KEY) or config.json.")
        sys.exit(1)
    parser = argparse.ArgumentParser(description="Dry-run rivalry scan of one match.")
    parser.add_argument("player_name", nargs="?", default="Human1zer",
                        help="scan this player's latest match (default: Human1zer)")
    parser.add_argument("--match", dest="match_id", help="scan this match ID instead")
    args = parser.parse_args()
    asyncio.run(main(args.player_name, args.match_id, api_key, config.get("request_delay", 9.0)))
