import asyncio
import logging
import traceback
from datetime import datetime, timezone
from typing import List, Optional, Set, Tuple

import discord
from discord.ext import commands, tasks

import database as db
from embeds import create_enhanced_match_embed, create_winner_embed
from tracker import AsyncPUBGMatchTracker
from weekly_stats import WeeklyStatsManager

logger = logging.getLogger(__name__)


class IntegratedPUBGBot:
    """Enhanced PUBG Discord Bot — async, SQLite-backed, with chicken dinner alerts."""

    def __init__(
        self,
        discord_token: str,
        channel_id: int,
        api_key: str,
        players: List[Tuple[str, str]],
        check_interval: int = 300,
        request_delay: float = 7.0,
        max_retries: int = 3,
        weekly_channel_id: int = None,
        winner_role_id: int = 0,
        posted_matches_max_history: int = 500,
    ):
        self.discord_token   = discord_token
        self.channel_id      = channel_id
        self.weekly_channel_id = weekly_channel_id or channel_id
        self.players         = players
        self.check_interval  = check_interval
        self.winner_role_id  = winner_role_id
        self.posted_max      = posted_matches_max_history
        self.players_file    = "players.txt"

        self.tracker       = AsyncPUBGMatchTracker(api_key, request_delay, max_retries)
        self.stats_manager = WeeklyStatsManager(max_history=posted_matches_max_history)

        # Posted-match set is loaded from SQLite in on_ready
        self.posted_matches: Set[str] = set()

        intents = discord.Intents.default()
        intents.message_content = True
        self.client = commands.Bot(command_prefix="!", intents=intents)
        self.client.event(self.on_ready)
        self._register_commands()

        self.cycle_number = 1
        self.is_running   = False

    # ─────────────────────────────────────────────────────────────────────────
    # Discord commands
    # ─────────────────────────────────────────────────────────────────────────

    def _register_commands(self):

        @self.client.command(name="addplayer")
        async def add_player(ctx, player_name: str):
            """Add a new player to track — !addplayer PlayerName"""
            if not ctx.author.guild_permissions.administrator:
                await ctx.send("❌ Only administrators can add players!")
                return
            for existing_name, _ in self.players:
                if existing_name.lower() == player_name.lower():
                    await ctx.send(f"❌ `{player_name}` is already being tracked!")
                    return
            self.players.append((player_name, "steam"))
            try:
                with open(self.players_file, "a", encoding="utf-8") as f:
                    f.write(f"\n{player_name}")
                await ctx.send(f"✅ Added `{player_name}`! Total tracked: {len(self.players)}")
                logger.info(f"✅ Added player: {player_name}")
            except Exception as e:
                await ctx.send(f"❌ Error saving player: {e}")
                self.players.remove((player_name, "steam"))

        @self.client.command(name="removeplayer")
        async def remove_player(ctx, player_name: str):
            """Remove a player — !removeplayer PlayerName"""
            if not ctx.author.guild_permissions.administrator:
                await ctx.send("❌ Only administrators can remove players!")
                return
            removed = None
            for player, platform in self.players[:]:
                if player.lower() == player_name.lower():
                    self.players.remove((player, platform))
                    removed = (player, platform)
                    break
            if not removed:
                await ctx.send(f"❌ `{player_name}` not found in tracking list!")
                return
            try:
                self._save_players_to_file()
                await ctx.send(f"✅ Removed `{player_name}`! Remaining: {len(self.players)}")
            except Exception as e:
                await ctx.send(f"❌ Error updating file: {e}")
                self.players.append(removed)

        @self.client.command(name="listplayers")
        async def list_players(ctx):
            """List all tracked players."""
            if not self.players:
                await ctx.send("📋 No players are currently being tracked.")
                return
            embed = discord.Embed(
                title="📋 Tracked Players",
                description=f"Total: {len(self.players)}",
                color=discord.Color.blue(),
            )
            lines = "\n".join(f"{i}. **{name}**" for i, (name, _) in enumerate(self.players, 1))
            embed.add_field(name="Players", value=lines, inline=False)
            await ctx.send(embed=embed)

        @self.client.command(name="best")
        async def best(ctx):
            """Show all-time personal best records for every tracked player."""
            rows = await db.get_all_time_best()
            if not rows:
                await ctx.send("⚠️ No match data in the database yet!")
                return
            embed = self.stats_manager.create_best_embed(rows)
            await ctx.send(embed=embed)

        @self.client.command(name="weeklynow")
        async def weekly_now(ctx):
            """Manually trigger the weekly summary — admin only."""
            if not ctx.author.guild_permissions.administrator:
                await ctx.send("❌ Only administrators can do this!")
                return
            await ctx.send("📊 Generating weekly summary…")
            weekly_data = await self.stats_manager.calculate_weekly_best_async(days=7)
            if not weekly_data:
                await ctx.send("⚠️ No data available for weekly stats!")
                return
            channel = self.client.get_channel(self.weekly_channel_id)
            if not channel:
                await ctx.send(f"❌ Channel not found! ID: `{self.weekly_channel_id}`")
                return
            try:
                await channel.send(embed=self.stats_manager.create_weekly_embed(weekly_data))
                await asyncio.sleep(4)
                await channel.send(embed=self.stats_manager.create_leaderboard_embed(weekly_data, top_n=5))
                await asyncio.sleep(4)
                alltime = self.stats_manager.create_alltime_kills_embed()
                if alltime:
                    await channel.send(embed=alltime)
                    await ctx.send("✅ Done — 3 embeds posted!")
                else:
                    await ctx.send("✅ Done — 2 embeds posted (run scrape_longest_kills.py to enable embed 3).")
            except Exception as e:
                await ctx.send(f"❌ Error: `{e}`")

        @self.client.command(name="testpost")
        async def test_post(ctx, player_name: str = None):
            """Generate a test embed and save to test_embed.txt."""
            if not player_name and self.players:
                player_name = self.players[0][0]
            elif not player_name:
                await ctx.send("❌ No players being tracked!")
                return
            fake_match = {
                "match_id": "test-match-id-000000000000",
                "match_category": "NORMAL",
                "game_mode": "squad",
                "map": "Baltic_Main",
                "duration_minutes": 28,
                "all_players_stats": {
                    player_name: {
                        "rank": 4, "kills": 3, "damage_dealt": 450.5,
                        "assists": 1, "dbnos": 2, "headshot_kills": 1,
                        "longest_kill": 187.3, "revives": 1, "revives_received": 0,
                        "heals_used": 3, "boosts_used": 2, "survival_time_minutes": 24.5,
                    }
                },
            }
            embed = create_enhanced_match_embed(fake_match, 1, 1)
            with open("test_embed.txt", "w", encoding="utf-8") as f:
                f.write(f"TITLE: {embed.title}\nDESC: {embed.description}\n\n")
                for field in embed.fields:
                    f.write(f"[{field.name}]\n{field.value}\n\n")
            await ctx.send(f"✅ Test embed for `{player_name}` saved to `test_embed.txt`")

    # ─────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    async def on_ready(self):
        logger.info(f"✅ Bot connected as {self.client.user}")
        logger.info(f"📡 Tracking {len(self.players)} players")
        logger.info(f"📢 Posting to channel: {self.channel_id}")
        logger.info(f"🔄 Poll interval: {self.check_interval}s")
        logger.info("💬 Commands: !addplayer !removeplayer !listplayers !best !weeklynow\n")

        # Load posted-match history from SQLite
        self.posted_matches = await db.load_posted_matches()

        if not self.is_running:
            # ── FIX: set the correct interval here, not inside the loop body ──
            self.check_matches_loop.change_interval(seconds=self.check_interval)
            self.check_matches_loop.start()
            self.weekly_summary_loop.start()
            self.is_running = True

    def run(self):
        try:
            self.client.run(self.discord_token)
        except Exception as e:
            logger.error(f"❌ Error starting bot: {e}")
            traceback.print_exc()
        finally:
            asyncio.run(self.tracker.close_session())

    # ─────────────────────────────────────────────────────────────────────────
    # Polling loop  (FIX: interval driven by tasks.loop, not asyncio.sleep)
    # ─────────────────────────────────────────────────────────────────────────

    @tasks.loop(seconds=300)   # default — overridden in on_ready with actual value
    async def check_matches_loop(self):
        try:
            logger.info(f"\n{'#'*80}")
            logger.info(f"# CYCLE {self.cycle_number} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info(f"{'#'*80}\n")

            self.tracker.reset_cycle()
            tracked_names = [name for name, _ in self.players]

            for idx, (player_name, platform) in enumerate(self.players, 1):
                logger.info(f"{'─'*80}")
                logger.info(f"[{idx}/{len(self.players)}] Fetching: {player_name}")
                logger.info(f"{'─'*80}")
                await self.tracker.get_latest_match(player_name, platform, tracked_names)
                if idx < len(self.players):
                    await asyncio.sleep(self.tracker.request_delay)

            self.tracker.print_cycle_summary(self.cycle_number)

            if self.tracker.results:
                await self._post_matches(self.tracker.results)

            self.cycle_number += 1
            logger.info(f"\n{'='*80}")
            logger.info(f"✅ Cycle {self.cycle_number - 1} done. Next in {self.check_interval}s…")
            logger.info(f"{'='*80}\n")

        except Exception as e:
            logger.error(f"❌ Error in check loop: {e}")
            traceback.print_exc()

    # ─────────────────────────────────────────────────────────────────────────
    # Weekly summary loop
    # ─────────────────────────────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def weekly_summary_loop(self):
        try:
            now = datetime.now(timezone.utc)
            if now.weekday() != 2 or now.hour != 18:
                return
            logger.info("\n" + "="*80)
            logger.info("📊 GENERATING WEEKLY SUMMARY — Wednesday 18:00 UTC")
            logger.info("="*80)

            weekly_data = await self.stats_manager.calculate_weekly_best_async(days=7)
            if not weekly_data:
                logger.warning("⚠️ No data for weekly summary")
                return

            channel = self.client.get_channel(self.weekly_channel_id)
            if not channel:
                logger.error(f"❌ Channel not found: {self.weekly_channel_id}")
                return

            await channel.send(embed=self.stats_manager.create_weekly_embed(weekly_data))
            await asyncio.sleep(4)
            await channel.send(embed=self.stats_manager.create_leaderboard_embed(weekly_data, top_n=5))
            await asyncio.sleep(4)
            alltime = self.stats_manager.create_alltime_kills_embed()
            if alltime:
                await channel.send(embed=alltime)
            logger.info("="*80 + "\n")

        except Exception as e:
            logger.error(f"❌ Error posting weekly summary: {e}")
            traceback.print_exc()

    # ─────────────────────────────────────────────────────────────────────────
    # Match posting
    # ─────────────────────────────────────────────────────────────────────────

    async def _post_matches(self, matches: List[dict]):
        try:
            channel = self.client.get_channel(self.channel_id)
            if not channel:
                logger.error(f"❌ Channel not found: {self.channel_id}")
                return

            new_matches = []
            newly_seen  = set()
            for match in matches:
                mid = match["match_id"]
                if mid not in self.posted_matches:
                    new_matches.append(match)
                    self.posted_matches.add(mid)
                    newly_seen.add(mid)
                else:
                    logger.info(f"⏭️ Skipping already-posted match: {mid[:16]}…")

            if not new_matches:
                logger.info("📭 No new matches to post")
                return

            # Persist the new IDs to SQLite
            await db.save_posted_matches(newly_seen, max_history=self.posted_max)

            logger.info(f"\n📤 Posting {len(new_matches)} new match(es) to Discord…")
            for idx, match in enumerate(new_matches, 1):
                await self._post_single_match(channel, match, idx, len(new_matches))
                if idx < len(new_matches):
                    await asyncio.sleep(4)

            logger.info(f"🎉 All {len(new_matches)} match(es) posted!")
            self._save_matches_for_stats(new_matches)

        except Exception as e:
            logger.error(f"❌ Error posting to Discord: {e}")
            traceback.print_exc()

    async def _post_single_match(self, channel, match: dict, idx: int, total: int):
        """Post one match embed, plus a chicken dinner alert if rank == 1."""
        embed = create_enhanced_match_embed(match, idx, total)
        if not embed:
            logger.warning(f"⚠️ Skipped match {idx}/{total}: no player data")
            return

        await channel.send(embed=embed)

        # ── Chicken dinner alert ─────────────────────────────────────────────
        winners = [
            name for name, stats in match.get("all_players_stats", {}).items()
            if stats.get("rank") == 1
        ]
        if winners:
            winner_embed = create_winner_embed(winners, match)
            mention = f"<@&{self.winner_role_id}> " if self.winner_role_id else ""
            await channel.send(content=mention if mention else None, embed=winner_embed)
            logger.info(f"🏆 Chicken dinner alert posted for: {', '.join(winners)}")

        players_in = list(match.get("all_players_stats", {}).keys())
        logger.info(f"✅ Posted {idx}/{total}: {', '.join(players_in)}")

    def _save_matches_for_stats(self, matches: List[dict]):
        try:
            individual = []
            for match in matches:
                for player_name, player_stats in match.get("all_players_stats", {}).items():
                    individual.append({
                        "player_name":       player_name,
                        "match_id":          match["match_id"],
                        "match_category":    match["match_category"],
                        "game_mode":         match["game_mode"],
                        "match_type":        match["match_type"],
                        "is_custom":         match["is_custom"],
                        "map":               match["map"],
                        "duration_seconds":  match["duration_seconds"],
                        "duration_minutes":  match["duration_minutes"],
                        "played_at":         match["played_at"],
                        "played_at_formatted": match["played_at_formatted"],
                        "player_stats":      player_stats,
                    })
            if individual:
                self.stats_manager.save_match_history(individual)
                logger.info(f"📊 Saved {len(individual)} player records to SQLite")
        except Exception as e:
            logger.error(f"⚠️ Error saving match history: {e}")
            traceback.print_exc()

    # ─────────────────────────────────────────────────────────────────────────
    # File helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _save_players_to_file(self):
        with open(self.players_file, "w", encoding="utf-8") as f:
            f.write("# PUBG Players to Track\n# Format: PlayerName (one per line)\n#\n")
            for name, _ in self.players:
                f.write(f"{name}\n")
