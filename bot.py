import asyncio
import logging
import traceback
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

import database as db
import shame
from embeds import (
    chunk_lines, create_enhanced_match_embed, create_winner_embed, paginate_embed, send_embeds,
)
from rivalry import WEEKLY_TOP_N, RivalryScanner, create_weekly_rivalry_embed
from tracker import AsyncPUBGMatchTracker
from weekly_stats import WeeklyStatsManager

logger = logging.getLogger(__name__)


class PUBGCog(commands.Cog, name="PUBGCog"):
    """PUBG match tracking, weekly stats, and chicken dinner alerts."""

    def __init__(
        self,
        bot: commands.Bot,
        channel_id: int,
        api_key: str,
        players: List[Tuple[str, str]],
        check_interval: int = 300,
        request_delay: float = 7.0,
        max_retries: int = 3,
        weekly_channel_id: int = None,
        winner_role_id: int = 0,
        winner_channel_id: int = 0,
        posted_matches_max_history: int = 500,
        players_file: str = "players.txt",
        shame_dry_run: bool = False,
        shame_top_n: int = 5,
        weekly_post_day: int = 6,
        weekly_post_hour: int = 18,
        weekly_post_timezone: str = "Europe/Oslo",
        rivalry_enabled: bool = True,
        rivalry_reserve_requests: int = 4,
        rivalry_cache_days: int = 7,
    ):
        self.bot              = bot
        self.channel_id       = channel_id
        self.weekly_channel_id = weekly_channel_id or channel_id
        self.players          = players
        self.check_interval   = check_interval
        self.winner_role_id   = winner_role_id
        self.winner_channel_id = winner_channel_id or channel_id
        self.posted_max       = posted_matches_max_history
        self.players_file     = players_file
        self.shame_dry_run = shame_dry_run
        self.shame_top_n   = shame_top_n
        # Shared schedule for the weekly summary + Wall of Shame run, both
        # posted to weekly_channel_id. weekly_post_day follows
        # datetime.weekday(): Monday=0 … Sunday=6. weekly_post_timezone is
        # an IANA zone name — using a local zone instead of UTC keeps the
        # wall-clock hour fixed across daylight saving changes.
        self.weekly_post_day  = weekly_post_day
        self.weekly_post_hour = weekly_post_hour
        self.weekly_post_tz   = ZoneInfo(weekly_post_timezone)

        self.tracker       = AsyncPUBGMatchTracker(api_key, request_delay, max_retries)
        self.stats_manager = WeeklyStatsManager(max_history=posted_matches_max_history)
        self.rivalry = RivalryScanner(
            self.tracker,
            lambda: [name for name, _ in self.players],
            reserve=rivalry_reserve_requests,
            cache_days=rivalry_cache_days,
        ) if rivalry_enabled else None

        # Posted-match set is loaded from SQLite in cog_load
        self.posted_matches: set = set()

        self.cycle_number = 1

    # ─────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    async def cog_load(self):
        self.posted_matches = await db.load_posted_matches()
        self.check_matches_loop.change_interval(seconds=self.check_interval)
        self.check_matches_loop.start()
        self.weekly_posts_loop.start()
        if self.rivalry:
            self.rivalry.start()

    async def cog_unload(self):
        self.check_matches_loop.cancel()
        self.weekly_posts_loop.cancel()
        if self.rivalry:
            await self.rivalry.stop()
        await self.tracker.close_session()

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"📡 PUBG cog ready — tracking {len(self.players)} players")
        logger.info(f"📢 Posting to channel: {self.channel_id}")
        logger.info(f"🔄 Poll interval: {self.check_interval}s")
        logger.info("💬 Commands: !addplayer !removeplayer !listplayers !best !rivalry !weeklynow !shame !shamenow !shametest\n")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        social_pattern = ("instagram.com", "instagr.am", "facebook.com", "fb.watch")
        if any(s in message.content.lower() for s in social_pattern):
            await message.delete()
            await message.channel.send(
                f"{message.author.mention}, Instagram/Facebook links aren't allowed here.",
                delete_after=5
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Commands
    # ─────────────────────────────────────────────────────────────────────────

    @commands.command(name="addplayer")
    async def add_player(self, ctx, player_name: str):
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

    @commands.command(name="removeplayer")
    async def remove_player(self, ctx, player_name: str):
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

    @commands.command(name="listplayers")
    async def list_players(self, ctx):
        """List all tracked players."""
        if not self.players:
            await ctx.send("📋 No players are currently being tracked.")
            return
        embed = discord.Embed(
            title="📋 Tracked Players",
            description=f"Total: {len(self.players)}",
            color=discord.Color.blue(),
        )
        lines = [f"{i}. **{name}**" for i, (name, _) in enumerate(self.players, 1)]
        for n, chunk in enumerate(chunk_lines(lines)):
            embed.add_field(name="Players" if n == 0 else "\u200b", value=chunk, inline=False)
        await send_embeds(ctx, paginate_embed(embed))

    @commands.command(name="best")
    async def best(self, ctx):
        """Show all-time personal best records for every tracked player."""
        rows = await db.get_all_time_best([name for name, _ in self.players])
        if not rows:
            await ctx.send("⚠️ No match data in the database yet!")
            return
        await send_embeds(ctx, self.stats_manager.create_best_embeds(rows))

    @commands.command(name="rivalry")
    async def rivalry_cmd(self, ctx, days: int = None):
        """Top rival clans vs tracked players — !rivalry [days]"""
        rows = await db.get_clan_rivalries(days=days, limit=10)
        if not rows:
            await ctx.send("⚔️ No cross-clan kills recorded yet!")
            return
        lines = []
        for i, r in enumerate(rows, 1):
            tag  = discord.utils.escape_markdown(r["clan_tag"] or "?")
            name = discord.utils.escape_markdown(r["clan_name"] or r["clan_id"])
            lines.append(f"{i}. **[{tag}]** {name} — {r['kills']} kills / {r['deaths']} deaths")
        embed = discord.Embed(
            title="⚔️ Clan Rivalries",
            description="\n".join(lines),
            color=discord.Color.dark_red(),
        )
        embed.set_footer(text=f"Last {days} days" if days else "All time")
        await ctx.send(embed=embed)

    @commands.command(name="weeklynow")
    async def weekly_now(self, ctx):
        """Manually trigger the weekly summary — admin only."""
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ Only administrators can do this!")
            return
        await ctx.send("📊 Generating weekly summary…")
        weekly_data = await self.stats_manager.calculate_weekly_best_async(days=7)
        if not weekly_data:
            await ctx.send("⚠️ No data available for weekly stats!")
            return
        channel = self.bot.get_channel(self.weekly_channel_id)
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
                await ctx.send("✅ Done — 2 embeds posted (run fetch_longest_kills.py to enable embed 3).")
        except Exception as e:
            await ctx.send(f"❌ Error: `{e}`")

    @commands.command(name="shame")
    async def shame_cmd(self, ctx):
        """Show the Wall of Shame — always posts to the weekly channel. Admin only."""
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ Only administrators can do this!")
            return
        await self._post_shame_board(ctx)

    @commands.command(name="shamenow")
    async def shame_now(self, ctx):
        """Force-post the Wall of Shame — admin only."""
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ Only administrators can do this!")
            return
        await self._post_shame_board(ctx)

    @commands.command(name="shametest")
    async def shame_test(self, ctx):
        """Preview the full weekly Wall of Shame post (embed + digest lines) — admin only. Respects shame_dry_run."""
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ Only administrators can do this!")
            return
        try:
            embed, digest_message = await self._build_weekly_shame_post()
        except Exception as e:
            await ctx.send(f"❌ Error: `{e}`")
            return

        if not embed and not digest_message:
            await ctx.send("⚠️ No shame-worthy plays in the last 7 days.")
            return

        if self.shame_dry_run:
            logger.info(
                "🔇 [shame dry-run] Weekly shame preview:\n"
                f"Embed fields: {[f.name for f in embed.fields] if embed else None}\n"
                f"{digest_message or ''}"
            )
            if embed:
                await ctx.send(embed=embed)
            if digest_message:
                await ctx.send(f"```\n{digest_message}\n```")
            await ctx.send("🔇 Dry-run — preview only, nothing posted to the weekly channel.")
            return

        channel = self.bot.get_channel(self.weekly_channel_id)
        if not channel:
            await ctx.send(f"❌ Channel not found! ID: `{self.weekly_channel_id}`")
            return
        if embed:
            await channel.send(embed=embed)
        if digest_message:
            await channel.send(digest_message)
        if channel.id != ctx.channel.id:
            await ctx.send(f"✅ Posted to {channel.mention}")
        else:
            await ctx.send("✅ Posted.")

    async def _post_shame_board(self, ctx):
        channel = self.bot.get_channel(self.weekly_channel_id)
        if not channel:
            await ctx.send(f"❌ Channel not found! ID: `{self.weekly_channel_id}`")
            return
        embed, digest_message = await self._build_weekly_shame_post()
        if not embed and not digest_message:
            await ctx.send("⚠️ No shame-worthy plays in the last 7 days.")
            return
        if embed:
            await channel.send(embed=embed)
        if digest_message:
            await channel.send(digest_message)
        if channel.id != ctx.channel.id:
            await ctx.send(f"✅ Posted to {channel.mention}")

    @commands.command(name="card")
    async def card_cmd(self, ctx, match_id: str = None):
        """Post telemetry match card(s) here — latest match if no ID. Admin only."""
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ Only administrators can do this!")
            return
        from scripts import match_card
        if not match_id:
            match_id = await asyncio.to_thread(match_card.latest_match_id)
            if not match_id:
                await ctx.send("⚠️ No match from the last 13 days in the DB.")
                return
        async with ctx.typing():
            try:
                cards = await asyncio.to_thread(match_card.render_cards, match_id)
            except Exception as e:
                logger.error(f"❌ !card failed for {match_id}: {e!r}")
                await ctx.send(f"❌ Card failed: `{e}`")
                return
        for c in cards:
            await ctx.send(file=discord.File(c))

    @commands.command(name="testpost")
    async def test_post(self, ctx, player_name: str = None):
        """Generate a test embed and save to test_embed.txt."""
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ Only administrators can do this!")
            return
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
    # Polling loop
    # ─────────────────────────────────────────────────────────────────────────

    @tasks.loop(seconds=300)   # default — overridden in cog_load with actual value
    async def check_matches_loop(self):
        try:
            logger.info(f"\n{'#'*80}")
            logger.info(f"# CYCLE {self.cycle_number} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info(f"{'#'*80}\n")

            self.tracker.reset_cycle()
            tracked_names = [name for name, _ in self.players]

            # Rivalry scanner holds off on rate-limited calls while we poll
            if self.rivalry:
                self.rivalry.tracker_busy()
            try:
                for idx, (player_name, platform) in enumerate(self.players, 1):
                    logger.info(f"{'─'*80}")
                    logger.info(f"[{idx}/{len(self.players)}] Fetching: {player_name}")
                    logger.info(f"{'─'*80}")
                    await self.tracker.get_latest_match(player_name, platform, tracked_names)
                    if idx < len(self.players):
                        await asyncio.sleep(self.tracker.request_delay)
            finally:
                if self.rivalry:
                    self.rivalry.tracker_idle()

            self.tracker.print_cycle_summary(self.cycle_number)

            if self.tracker.results:
                await self._post_matches(self.tracker.results)
                if self.rivalry:
                    for match in self.tracker.results:
                        await self.rivalry.enqueue(match)

            self.cycle_number += 1
            logger.info(f"\n{'='*80}")
            logger.info(f"✅ Cycle {self.cycle_number - 1} done. Next in {self.check_interval}s…")
            logger.info(f"{'='*80}\n")

        except Exception as e:
            logger.error(f"❌ Error in check loop: {e}")
            traceback.print_exc()

    @check_matches_loop.before_loop
    async def before_check_matches(self):
        await self.bot.wait_until_ready()

    # ─────────────────────────────────────────────────────────────────────────
    # Weekly posts — weekly summary + Wall of Shame + clan rivalries,
    # same run, same schedule
    # ─────────────────────────────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def weekly_posts_loop(self):
        try:
            now = datetime.now(timezone.utc).astimezone(self.weekly_post_tz)
            if now.weekday() != self.weekly_post_day or now.hour < self.weekly_post_hour:
                return

            week_key = now.strftime("%G-W%V")

            # ── Weekly summary (restart-safe: only post once per ISO week) ───
            if await db.get_state("last_weekly_post") != week_key:
                logger.info("\n" + "="*80)
                logger.info("📊 GENERATING WEEKLY SUMMARY")
                logger.info("="*80)

                weekly_data = await self.stats_manager.calculate_weekly_best_async(days=7)
                if not weekly_data:
                    logger.warning("⚠️ No data for weekly summary")
                else:
                    channel = self.bot.get_channel(self.weekly_channel_id)
                    if not channel:
                        logger.error(f"❌ Channel not found: {self.weekly_channel_id}")
                    else:
                        await channel.send(embed=self.stats_manager.create_weekly_embed(weekly_data))
                        await asyncio.sleep(4)
                        await channel.send(embed=self.stats_manager.create_leaderboard_embed(weekly_data, top_n=5))
                        await asyncio.sleep(4)
                        alltime = self.stats_manager.create_alltime_kills_embed()
                        if alltime:
                            await channel.send(embed=alltime)
                        await db.set_state("last_weekly_post", week_key)
                logger.info("="*80 + "\n")

            # ── Wall of Shame, right after (restart-safe, same week guard) ───
            if await db.get_state("last_shame_post") != week_key:
                channel = self.bot.get_channel(self.weekly_channel_id)
                if not channel:
                    logger.error(f"❌ Channel not found: {self.weekly_channel_id}")
                else:
                    embed, digest_message = await self._build_weekly_shame_post()
                    if not embed and not digest_message:
                        logger.warning("⚠️ No shame-worthy plays this week — skipping Wall of Shame")
                    else:
                        if embed:
                            await channel.send(embed=embed)
                        if digest_message:
                            await channel.send(digest_message)
                        await db.set_state("last_shame_post", week_key)

            # ── Clan rivalries, last (restart-safe, same week guard) ─────────
            if self.rivalry and await db.get_state("last_rivalry_post") != week_key:
                rows = await db.get_clan_rivalries(days=7, limit=WEEKLY_TOP_N)
                if not rows:
                    logger.info("⚔️ No cross-clan kills this week — skipping rivalry section")
                else:
                    channel = self.bot.get_channel(self.weekly_channel_id)
                    if not channel:
                        logger.error(f"❌ Channel not found: {self.weekly_channel_id}")
                    else:
                        await channel.send(embed=create_weekly_rivalry_embed(rows, days=7))
                        await db.set_state("last_rivalry_post", week_key)

        except Exception as e:
            logger.error(f"❌ Error posting weekly summary/Wall of Shame/rivalries: {e}")
            traceback.print_exc()

    @weekly_posts_loop.before_loop
    async def before_weekly_posts(self):
        await self.bot.wait_until_ready()

    async def _build_weekly_shame_post(self, days: int = 7) -> Tuple[Optional[discord.Embed], Optional[str]]:
        """
        Builds the Wall-of-Shame post: the awards embed, plus the digest
        lines for the same window that get posted right after it. Shared
        by the weekly loop, !shame/!shamenow, and !shametest so they all
        render the same thing. Either element may be None if there was
        nothing to report for that half.
        """
        result = await shame.calculate_weekly_shame(days=days, top_n=self.shame_top_n)
        embed = shame.create_weekly_shame_embed(result[0], result[1], days=days) if result else None

        # Digest feed disabled — only the awards embed gets posted.
        digest_message = None

        return embed, digest_message

    # ─────────────────────────────────────────────────────────────────────────
    # Match posting
    # ─────────────────────────────────────────────────────────────────────────

    async def _post_matches(self, matches: List[dict]):
        try:
            channel = self.bot.get_channel(self.channel_id)
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
            await self._save_matches_for_stats(new_matches)

        except Exception as e:
            logger.error(f"❌ Error posting to Discord: {e}")
            traceback.print_exc()

    async def _post_single_match(self, channel, match: dict, idx: int, total: int):
        """Post one match embed, plus a chicken dinner alert if rank == 1."""
        embed = create_enhanced_match_embed(match, idx, total)
        if not embed:
            logger.warning(f"⚠️ Skipped match {idx}/{total}: no player data")
            return

        cards = await self._render_cards(match)
        if cards:
            for c in cards:
                await channel.send(file=discord.File(c))
        else:
            await send_embeds(channel, paginate_embed(embed))

        # ── Chicken dinner alert ─────────────────────────────────────────────
        winners = [
            name for name, stats in match.get("all_players_stats", {}).items()
            if stats.get("rank") == 1
        ]
        if winners:
            winner_embed = create_winner_embed(winners, match)
            mention = f"<@&{self.winner_role_id}> " if self.winner_role_id else ""

            winner_channel = self.bot.get_channel(self.winner_channel_id) or channel

            win_cards = [c for c in cards if c.name.endswith("-1.png")]
            if win_cards:
                for c in win_cards:
                    await winner_channel.send(content=mention or None, file=discord.File(c))
            else:
                await send_embeds(winner_channel, paginate_embed(winner_embed), content=mention or None)
            logger.info(f"🏆 Chicken dinner alert posted for: {', '.join(winners)}")

            birthday_cog = self.bot.get_cog("BirthdayCog")
            if birthday_cog:
                await birthday_cog.check_pubg_birthday_dinner(match)

        players_in = list(match.get("all_players_stats", {}).keys())
        logger.info(f"✅ Posted {idx}/{total}: {', '.join(players_in)}")

    async def _render_cards(self, match: dict):
        """Telemetry match card(s) for this match; [] means fall back to the embed."""
        try:
            from scripts import match_card
            players = list(match.get("all_players_stats", {}).keys())
            return await asyncio.wait_for(
                asyncio.to_thread(match_card.render_cards, match["match_id"], players), timeout=300)
        except Exception as e:
            logger.warning(f"⚠️ Match card failed for {str(match.get('match_id'))[:16]}, using embed: {e!r}")
            return []

    async def _save_matches_for_stats(self, matches: List[dict]):
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
                await db.save_match_history(individual)
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


async def setup(bot: commands.Bot, **kwargs):
    await bot.add_cog(PUBGCog(bot, **kwargs))
