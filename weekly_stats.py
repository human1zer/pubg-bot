"""
weekly_stats.py — Weekly statistics manager for PUBG Bot

Storage backend is now SQLite (via database.py).
All embed builders and the WeeklyStatsManager public API are unchanged
so bot.py requires no edits to its weekly-summary calls.
"""

import asyncio
import logging
import os
import traceback
from datetime import datetime, timezone
from typing import Optional

import discord

import database as db

logger = logging.getLogger(__name__)


class WeeklyStatsManager:
    """Manages match history and weekly statistics.  Backed by SQLite."""

    def __init__(self, max_history: int = 500):
        # max_history is kept for API compatibility but is enforced in database.py
        self.max_history = max_history

    # ── Write path ───────────────────────────────────────────────────────────

    def save_match_history(self, new_matches: list) -> None:
        """
        Synchronous wrapper used by bot.py (called from a non-async context).
        Spawns a new event loop if necessary.
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're inside an async context — schedule as a task
                asyncio.ensure_future(db.save_match_history(new_matches))
            else:
                loop.run_until_complete(db.save_match_history(new_matches))
        except RuntimeError:
            asyncio.run(db.save_match_history(new_matches))

    # ── Read path ────────────────────────────────────────────────────────────

    def calculate_weekly_best(self, days: int = 7) -> Optional[dict]:
        """Synchronous wrapper — runs the async query and crunches the numbers."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Can't block a running loop; caller should use the async version
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(asyncio.run, db.get_matches_since(days))
                    rows = future.result(timeout=30)
            else:
                rows = loop.run_until_complete(db.get_matches_since(days))
        except Exception as e:
            logger.error(f"⚠️ Error fetching matches from DB: {e}")
            return None

        if not rows:
            logger.warning(f"⚠️ No matches found in the last {days} days")
            return None

        logger.info(f"📊 Analysing {len(rows)} matches from the last {days} days...")
        return self._crunch(rows, days)

    async def calculate_weekly_best_async(self, days: int = 7) -> Optional[dict]:
        """Async version for use inside coroutines (e.g. !weeklynow handler)."""
        rows = await db.get_matches_since(days)
        if not rows:
            logger.warning(f"⚠️ No matches found in the last {days} days")
            return None
        logger.info(f"📊 Analysing {len(rows)} matches from the last {days} days...")
        return self._crunch(rows, days)

    # ── Core calculation ─────────────────────────────────────────────────────

    def _crunch(self, rows: list, days: int) -> Optional[dict]:
        player_stats: dict = {}
        longest_kills_pool: list = []

        for row in rows:
            player = row["player_name"]
            if player not in player_stats:
                player_stats[player] = {
                    "matches": 0, "total_kills": 0, "total_damage": 0,
                    "total_survival": 0, "wins": 0, "top_5": 0, "top_10": 0,
                    "total_headshots": 0, "total_assists": 0, "total_dbnos": 0,
                    "best_kills": 0, "best_damage": 0, "best_survival": 0,
                    "total_distance": 0, "longest_kill": 0,
                }

            ps = player_stats[player]
            ps["matches"]         += 1
            ps["total_kills"]     += row["kills"]
            ps["total_damage"]    += row["damage_dealt"]
            ps["total_survival"]  += row["survival_time_minutes"]
            ps["total_headshots"] += row["headshot_kills"]
            ps["total_assists"]   += row["assists"]
            ps["total_dbnos"]     += row["dbnos"]
            ps["total_distance"]  += (row["walk_distance"] + row["ride_distance"]) / 1000

            rank = row["rank"]
            if rank == 1:  ps["wins"]   += 1
            if rank <= 5:  ps["top_5"]  += 1
            if rank <= 10: ps["top_10"] += 1

            if row["kills"]               > ps["best_kills"]:    ps["best_kills"]    = row["kills"]
            if row["damage_dealt"]        > ps["best_damage"]:   ps["best_damage"]   = row["damage_dealt"]
            if row["survival_time_minutes"] > ps["best_survival"]:ps["best_survival"] = row["survival_time_minutes"]
            if row["longest_kill"]        > ps["longest_kill"]:  ps["longest_kill"]  = row["longest_kill"]

            if row["longest_kill"] > 0:
                longest_kills_pool.append((row["longest_kill"], player))

        if not player_stats:
            return None

        # Top-3 longest kills (one entry per player)
        longest_kills_pool.sort(key=lambda x: x[0], reverse=True)
        seen_players: set = set()
        top3_longest: list = []
        for dist, pname in longest_kills_pool:
            if pname not in seen_players:
                top3_longest.append({"player": pname, "distance": dist})
                seen_players.add(pname)
                if len(top3_longest) == 3:
                    break

        # Per-player averages and composite score
        for player, s in player_stats.items():
            m = s["matches"]
            s["avg_kills"]    = round(s["total_kills"]    / m, 2)
            s["avg_damage"]   = round(s["total_damage"]   / m, 2)
            s["avg_survival"] = round(s["total_survival"] / m, 2)
            s["avg_distance"] = round(s["total_distance"] / m, 2)
            s["win_rate"]     = round((s["wins"] / m) * 100, 1)
            s["top_5_rate"]   = round((s["top_5"] / m) * 100, 1)
            s["score"] = (
                s["avg_kills"]      * 100 +
                s["avg_damage"]     * 0.5 +
                s["wins"]           * 500 +
                s["top_5"]          * 100 +
                s["top_10"]         * 50  +
                s["avg_survival"]   * 10  +
                s["total_headshots"]* 20
            )

        sorted_players = sorted(player_stats.items(), key=lambda x: x[1]["score"], reverse=True)
        best_player    = sorted_players[0]

        return {
            "player":             best_player[0],
            "stats":              best_player[1],
            "top3_longest_kills": top3_longest,
            "all_players":        dict(sorted_players),
            "days":               days,
            "total_matches":      len(rows),
        }

    # ── Embed builders (unchanged from original) ─────────────────────────────

    def create_weekly_embed(self, weekly_data: dict) -> discord.Embed:
        player = weekly_data["player"]
        stats  = weekly_data["stats"]
        days   = weekly_data["days"]
        top3   = weekly_data.get("top3_longest_kills", [])

        embed = discord.Embed(
            title=f"🏆 Best Player — Last {days} Days",
            description=f"**{player}** dominated the battlefield!",
            color=discord.Color.gold(),
            timestamp=datetime.now(),
        )
        embed.add_field(
            name="📋 Matches Played",
            value=(
                f"**Total:** {stats['matches']}\n"
                f"**Wins:** {stats['wins']} 🏆\n"
                f"**Top 5:** {stats['top_5']} ({stats['top_5_rate']}%)\n"
                f"**Win Rate:** {stats['win_rate']}%"
            ),
            inline=True,
        )
        embed.add_field(
            name="⚔️ Combat Stats",
            value=(
                f"**Avg Kills:** {stats['avg_kills']}\n"
                f"**Best Game:** {stats['best_kills']} kills\n"
                f"**Total Kills:** {stats['total_kills']}\n"
                f"**Headshots:** {stats['total_headshots']}"
            ),
            inline=True,
        )
        embed.add_field(
            name="💥 Damage Dealt",
            value=(
                f"**Avg:** {stats['avg_damage']}\n"
                f"**Best:** {round(stats['best_damage'], 0)}\n"
                f"**Total:** {round(stats['total_damage'], 0)}"
            ),
            inline=True,
        )
        embed.add_field(
            name="🤝 Support",
            value=f"**Assists:** {stats['total_assists']}\n**Knockdowns:** {stats['total_dbnos']}",
            inline=True,
        )
        embed.add_field(
            name="⏱️ Survival Time",
            value=(
                f"**Avg:** {stats['avg_survival']} min\n"
                f"**Best:** {round(stats['best_survival'], 1)} min\n"
                f"**Total:** {round(stats['total_survival'] / 60, 1)} hours"
            ),
            inline=True,
        )
        embed.add_field(
            name="🗺️ Distance",
            value=f"**Avg:** {stats['avg_distance']} km\n**Total:** {round(stats['total_distance'], 1)} km",
            inline=True,
        )
        embed.add_field(
            name="🎯 Overall Score",
            value=f"**{round(stats['score'], 0)}** points",
            inline=False,
        )

        medals = ["🥇", "🥈", "🥉"]
        if top3:
            lines = [
                f"{medals[i] if i < 3 else f'{i+1}.'} **{e['player']}**: {round(e['distance'], 0)}m"
                for i, e in enumerate(top3)
            ]
            embed.add_field(name="🔭 Top 3 Longest Kills of the Week", value="\n".join(lines), inline=False)

        embed.set_footer(text=f"Calculated from {weekly_data['total_matches']} matches")
        return embed

    def create_leaderboard_embed(self, weekly_data: dict, top_n: int = 5) -> discord.Embed:
        days        = weekly_data["days"]
        all_players = weekly_data["all_players"]
        medals      = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

        embed = discord.Embed(
            title=f"📊 Leaderboard — Last {days} Days",
            description=f"Top {min(top_n, len(all_players))} Players",
            color=discord.Color.blue(),
            timestamp=datetime.now(),
        )
        for idx, (player, stats) in enumerate(list(all_players.items())[:top_n]):
            medal = medals[idx] if idx < len(medals) else f"{idx+1}."
            embed.add_field(
                name=f"{medal} {player}",
                value=(
                    f"**Score:** {round(stats['score'], 0)}\n"
                    f"**Matches:** {stats['matches']} | **Wins:** {stats['wins']}\n"
                    f"**Avg K/D:** {stats['avg_kills']} | **Dmg:** {round(stats['avg_damage'], 0)}"
                ),
                inline=False,
            )
        embed.set_footer(text=f"Based on {weekly_data['total_matches']} total matches")
        return embed

    def create_alltime_kills_embed(self, top_n: int = 10):
        """Builds all-time longest kills embed from the lifetime_longest_kills DB table."""
        import asyncio
        import concurrent.futures

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    from fetch_longest_kills import get_from_db
                    future = pool.submit(asyncio.run, get_from_db())
                    rows = future.result(timeout=30)
            else:
                from fetch_longest_kills import get_from_db
                rows = loop.run_until_complete(get_from_db())
        except Exception as e:
            logger.warning(f"⚠️ Could not load lifetime kills from DB: {e}")
            return None

        if not rows:
            return None

        medals = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, top_n + 1)]
        embed = discord.Embed(
            title="🔭 All-Time Top Longest Kills",
            description=f"Best sniper shot ever recorded per player — Top {min(top_n, len(rows))}",
            color=discord.Color.from_rgb(138, 43, 226),
            timestamp=datetime.now(),
        )
        lines = [
            f"{medals[i] if i < len(medals) else f'{i+1}.'} **{r['player_name']}** — {r['longest_kill']:.1f}m"
            for i, r in enumerate(rows[:top_n])
        ]
        embed.add_field(name="🎯 Rankings", value="\n".join(lines) or "No data yet", inline=False)
        embed.set_footer(text=f"Updated via PUBG Lifetime API • {datetime.now().strftime('%Y-%m-%d')}")
        return embed

    # ── !best embed ──────────────────────────────────────────────────────────

    def create_best_embed(self, rows: list) -> discord.Embed:
        """
        Build the !best embed from get_all_time_best() rows.
        Shows each player's all-time personal records.
        """
        embed = discord.Embed(
            title="🏅 All-Time Personal Bests",
            description="Best single-match records for every tracked player",
            color=discord.Color.from_rgb(255, 165, 0),
            timestamp=datetime.now(),
        )

        kill_leader   = max(rows, key=lambda r: r["best_kills"])
        damage_leader = max(rows, key=lambda r: r["best_damage"])
        snipe_leader  = max(rows, key=lambda r: r["longest_kill"])
        win_leader    = max(rows, key=lambda r: r["total_wins"])

        embed.add_field(
            name="🏆 All-Time Records",
            value=(
                f"💀 **Most kills in one game:** {kill_leader['best_kills']} — *{kill_leader['player_name']}*\n"
                f"💥 **Most damage in one game:** {round(damage_leader['best_damage'], 0):,} — *{damage_leader['player_name']}*\n"
                f"🔭 **Longest kill shot:** {round(snipe_leader['longest_kill'], 0)}m — *{snipe_leader['player_name']}*\n"
                f"🥇 **Most wins:** {win_leader['total_wins']} — *{win_leader['player_name']}*"
            ),
            inline=False,
        )

        medals = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, len(rows) + 1)]
        for i, row in enumerate(rows):
            win_rate = round((row["total_wins"] / row["total_matches"] * 100), 1) if row["total_matches"] else 0
            embed.add_field(
                name=f"{medals[i] if i < len(medals) else f'{i+1}.'} {row['player_name']}",
                value=(
                    f"🎮 {row['total_matches']} matches | 🏆 {row['total_wins']} wins ({win_rate}%)\n"
                    f"💀 Best game: **{row['best_kills']}** kills | "
                    f"💥 **{round(row['best_damage'], 0):,}** dmg | "
                    f"🔭 **{round(row['longest_kill'], 0)}m**"
                ),
                inline=False,
            )

        embed.set_footer(text="Across all tracked matches in the database")
        return embed

    # ── Utility ──────────────────────────────────────────────────────────────

    def get_player_summary(self, player_name: str, days: int = 7):
        weekly_data = self.calculate_weekly_best(days)
        if not weekly_data:
            return None
        for player, stats in weekly_data["all_players"].items():
            if player.lower() == player_name.lower():
                rank = list(weekly_data["all_players"].keys()).index(player) + 1
                return {
                    "player": player, "stats": stats,
                    "rank": rank, "total_players": len(weekly_data["all_players"]),
                    "days": days,
                }
        return None

    def create_player_summary_embed(self, player_data: dict) -> discord.Embed:
        player = player_data["player"]
        stats  = player_data["stats"]
        rank   = player_data["rank"]
        total  = player_data["total_players"]
        days   = player_data["days"]

        if rank == 1:    color = discord.Color.gold()
        elif rank <= 3:  color = discord.Color.green()
        elif rank <= 5:  color = discord.Color.blue()
        else:            color = discord.Color.greyple()

        embed = discord.Embed(
            title=f"📊 {player} — {days} Day Summary",
            description=f"Rank: **#{rank}** out of {total} players",
            color=color,
            timestamp=datetime.now(),
        )
        embed.add_field(
            name="🎮 Performance",
            value=(
                f"**Matches:** {stats['matches']}\n"
                f"**Wins:** {stats['wins']} ({stats['win_rate']}%)\n"
                f"**Top 5:** {stats['top_5']} ({stats['top_5_rate']}%)\n"
                f"**Score:** {round(stats['score'], 0)}"
            ),
            inline=True,
        )
        embed.add_field(
            name="⚔️ Combat",
            value=(
                f"**Avg Kills:** {stats['avg_kills']}\n"
                f"**Best:** {stats['best_kills']} kills\n"
                f"**Headshots:** {stats['total_headshots']}\n"
                f"**Avg Dmg:** {round(stats['avg_damage'], 0)}"
            ),
            inline=True,
        )
        embed.add_field(
            name="📈 Stats",
            value=(
                f"**Avg Survival:** {stats['avg_survival']} min\n"
                f"**Assists:** {stats['total_assists']}\n"
                f"**KDs:** {stats['total_dbnos']}\n"
                f"**Distance:** {stats['avg_distance']} km"
            ),
            inline=True,
        )
        return embed
