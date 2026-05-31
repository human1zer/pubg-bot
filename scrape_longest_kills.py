"""
scrape_longest_kills.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scans your Discord channel history, finds all PUBG match
embeds posted by the bot, and extracts the longest kill
distance per player to build an all-time top-10 leaderboard.

Saves results to:  longest_kills_alltime.json

Run:  python scrape_longest_kills.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import discord
import asyncio
import json
import re
import os
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
with open("config.json") as f:
    config = json.load(f)

TOKEN      = config["discord_token"]
CHANNEL_ID = config["discord_channel_id"]   # channel to scan
OUTPUT_FILE = "longest_kills_alltime.json"
TOP_N = 10   # how many entries to keep in the leaderboard
# ──────────────────────────────────────────────────────────────────────────────


def parse_embeds(message: discord.Message) -> list[dict]:
    """
    Extract (player_name, longest_kill, match_date) from a single Discord message.
    Returns a list because one message can have multiple player fields.
    """
    results = []

    for embed in message.embeds:
        # Only process match embeds (they have a title with NORMAL/RANKED/etc.)
        if not embed.title:
            continue

        # Try to get match date from embed timestamp
        match_date = embed.timestamp.isoformat() if embed.timestamp else message.created_at.isoformat()

        for field in embed.fields:
            # Player fields are named like "👤 K9-UNITT"
            if not field.name or "👤" not in field.name:
                continue

            player_name = field.name.replace("👤", "").strip()

            # Look for "Longest:      535m" inside the css code block
            # The format is:  Longest:    <number>m
            match = re.search(r"Longest:\s+([\d.]+)m", field.value)
            if match:
                distance = float(match.group(1))
                if distance > 0:
                    results.append({
                        "player":   player_name,
                        "distance": distance,
                        "date":     match_date,
                        "message_id": message.id
                    })

    return results


async def scrape():
    intents = discord.Intents.default()
    client  = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"✅ Logged in as {client.user}")
        print(f"📡 Scanning channel ID: {CHANNEL_ID}")
        print("━" * 60)

        channel = client.get_channel(CHANNEL_ID)
        if not channel:
            print(f"❌ Channel {CHANNEL_ID} not found!")
            await client.close()
            return

        # ── Load existing data so we don't lose previous scrapes ──────────────
        all_entries: list[dict] = []
        seen_message_ids: set[int] = set()

        if os.path.exists(OUTPUT_FILE):
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
            all_entries = existing.get("raw_entries", [])
            seen_message_ids = {e["message_id"] for e in all_entries}
            print(f"📂 Loaded {len(all_entries)} existing entries from {OUTPUT_FILE}")
        else:
            print(f"📂 No existing file — starting fresh")

        # ── Scan channel history ───────────────────────────────────────────────
        scanned   = 0
        new_found = 0

        async for message in channel.history(limit=None, oldest_first=True):
            scanned += 1

            if scanned % 200 == 0:
                print(f"   ... scanned {scanned} messages, found {new_found} new kill entries so far")

            # Skip messages we've already processed
            if message.id in seen_message_ids:
                continue

            # Only process messages that have embeds
            if not message.embeds:
                continue

            entries = parse_embeds(message)
            if entries:
                all_entries.extend(entries)
                seen_message_ids.update(e["message_id"] for e in entries)
                new_found += len(entries)

        print(f"\n✅ Scan complete!")
        print(f"   Messages scanned : {scanned}")
        print(f"   New kill entries : {new_found}")
        print(f"   Total entries    : {len(all_entries)}")

        # ── Build top-N leaderboard (best per player, then overall top-N) ─────
        # Step 1: best distance per player across ALL their entries
        best_per_player: dict[str, dict] = {}
        for entry in all_entries:
            p = entry["player"]
            if p not in best_per_player or entry["distance"] > best_per_player[p]["distance"]:
                best_per_player[p] = entry

        # Step 2: sort by distance and take top N
        top_n = sorted(best_per_player.values(), key=lambda x: x["distance"], reverse=True)[:TOP_N]

        # ── Pretty print ──────────────────────────────────────────────────────
        medals = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, TOP_N + 1)]
        print(f"\n{'━'*60}")
        print(f"  🔭 ALL-TIME TOP {TOP_N} LONGEST KILLS")
        print(f"{'━'*60}")
        for i, entry in enumerate(top_n):
            medal = medals[i]
            date  = entry["date"][:10]
            print(f"  {medal:<4} {entry['player']:<20}  {entry['distance']:>7.1f}m   ({date})")
        print(f"{'━'*60}\n")

        # ── Save to JSON ──────────────────────────────────────────────────────
        output = {
            "generated_at":    datetime.now().isoformat(),
            "total_entries":   len(all_entries),
            "messages_scanned": scanned,
            f"top_{TOP_N}":    top_n,
            "raw_entries":     all_entries   # keep raw so future runs can skip already-seen messages
        }

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2)

        print(f"💾 Saved to {OUTPUT_FILE}")
        await asyncio.sleep(0.25)  # ← أضف هذا
        await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(scrape())
