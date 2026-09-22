"""
clan_kill_scanner.py — one-off utility, not part of the bot's runtime.

Scans the most recent match of a given player for cross-clan kills
(kills where the killer and victim belong to different PUBG clans).

Usage:
    python scripts/clan_kill_scanner.py [player_name]

Requires PUBG_API_KEY in .env (see .env.example) or config.json — same
credentials as the rest of the bot. `requests` must be installed
(it isn't a runtime dependency of the bot itself).
"""

import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import load_config  # noqa: E402

PLATFORM = "steam"

config = load_config()
if not config:
    sys.exit(1)

API_KEY = config.get("pubg_api_key")
if not API_KEY or API_KEY == "YOUR_PUBG_API_KEY_HERE":
    print("❌ PUBG API key not set. Add it to .env (PUBG_API_KEY) or config.json.")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/vnd.api+json",
}
BASE = f"https://api.pubg.com/shards/{PLATFORM}"


def get_clan_tag(account_id):
    """Fetch clanTag for a single player account."""
    r = requests.get(f"{BASE}/players/{account_id}", headers=HEADERS)
    if r.status_code != 200:
        return None
    attrs = r.json()["data"]["attributes"]
    clan_id = attrs.get("clanId")
    if not clan_id:
        return None
    r2 = requests.get(f"{BASE}/clans/{clan_id}", headers=HEADERS)
    if r2.status_code != 200:
        return None
    return r2.json()["data"]["attributes"].get("clanTag")


def main():
    player_name = sys.argv[1] if len(sys.argv) > 1 else "Human1zer"

    # Step 1 — get latest match
    print(f"[1] Fetching latest match for {player_name}...")
    r = requests.get(f"{BASE}/players?filter[playerNames]={player_name}", headers=HEADERS)
    player    = r.json()["data"][0]
    match_id  = player["relationships"]["matches"]["data"][0]["id"]
    print(f"  Match ID: {match_id}")

    # Step 2 — get all players in match (account_id + name)
    print(f"\n[2] Fetching match data...")
    r = requests.get(f"{BASE}/matches/{match_id}", headers=HEADERS)
    match_data = r.json()

    # Get telemetry URL + map
    map_name = match_data["data"]["attributes"].get("mapName", "Unknown")
    tel_url  = None
    for item in match_data.get("included", []):
        if item["type"] == "asset":
            tel_url = item["attributes"]["URL"]

    # Build name -> account_id map from participants
    name_to_account = {}
    for item in match_data.get("included", []):
        if item["type"] == "participant":
            attrs = item["attributes"]["stats"]
            name  = attrs.get("name")
            pid   = attrs.get("playerId")  # account.xxxx
            if name and pid and pid.startswith("account."):
                name_to_account[name] = pid

    print(f"  Players in match: {len(name_to_account)}")
    print(f"  Map: {map_name}")

    # Step 3 — fetch clan for each player (only first 20 to avoid rate limit)
    print(f"\n[3] Fetching clan tags (first 20 players)...")
    name_to_clan = {}
    players_list = list(name_to_account.items())[:20]

    for i, (name, account_id) in enumerate(players_list):
        tag = get_clan_tag(account_id)
        name_to_clan[name] = tag
        status = f"[{tag}]" if tag else "no clan"
        print(f"  {i+1:2}. {name:30s} → {status}")
        time.sleep(0.2)  # be nice to the API

    # Step 4 — scan kills using clan lookup
    print(f"\n[4] Fetching telemetry + scanning kills...")
    r = requests.get(tel_url)
    telemetry = r.json()
    kills = [e for e in telemetry if e.get("_T") == "LogPlayerKillV2"]

    cross_clan_kills = []
    for k in kills:
        killer_name = (k.get("killer") or {}).get("name", "")
        victim_name = (k.get("victim") or {}).get("name", "")
        k_clan = name_to_clan.get(killer_name)
        v_clan = name_to_clan.get(victim_name)

        if k_clan and v_clan and k_clan != v_clan:
            dmg  = k.get("killerDamageInfo") or {}
            dist = dmg.get("distance", 0) / 100
            cross_clan_kills.append({
                "killer": killer_name, "killer_clan": k_clan,
                "victim": victim_name, "victim_clan": v_clan,
                "distance": round(dist, 1), "map": map_name
            })

    print(f"\n{'=' * 60}")
    print(f"CROSS-CLAN KILLS FOUND: {len(cross_clan_kills)}")
    for k in cross_clan_kills:
        print(f"  💀 [{k['killer_clan']}]{k['killer']} killed [{k['victim_clan']}]{k['victim']} | {k['distance']}m")

    print(f"\nCLAN TAGS SEEN IN THIS MATCH:")
    clans_seen = set(v for v in name_to_clan.values() if v)
    for c in sorted(clans_seen):
        print(f"  [{c}]")
    print("=" * 60)


if __name__ == "__main__":
    main()
