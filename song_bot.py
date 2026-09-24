import datetime
import logging
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

logger = logging.getLogger(__name__)

MESSAGE = (
    "📢 **Song Check!** 🎧\n"
    "@everyone\n\n"
    "Drop the **last song you listened to** 👇\n"
    "No skipping, no lying, whatever it was, post it 😂\n\n"
    "Link it (Spotify / YouTube) or just type the name."
)


class SongCog(commands.Cog):
    def __init__(self, bot, channel_id, post_day, post_hour, timezone):
        self.bot = bot
        self.channel_id = channel_id
        self.post_day = post_day          # Monday=0 ... Sunday=6
        self.tz = ZoneInfo(timezone)
        self.weekly_post.change_interval(
            time=datetime.time(hour=post_hour, tzinfo=self.tz)
        )

    async def cog_load(self):
        self.weekly_post.start()

    async def cog_unload(self):
        self.weekly_post.cancel()

    @tasks.loop(time=datetime.time(hour=0))  # overridden in __init__
    async def weekly_post(self):
        if datetime.datetime.now(self.tz).weekday() != self.post_day:
            return
        await self.post()

    @weekly_post.before_loop
    async def before_weekly_post(self):
        await self.bot.wait_until_ready()

    async def post(self):
        channel = self.bot.get_channel(self.channel_id) or await self.bot.fetch_channel(self.channel_id)
        msg = await channel.send(
            MESSAGE, allowed_mentions=discord.AllowedMentions(everyone=True)
        )
        await msg.create_thread(name="🎵 Last Song", auto_archive_duration=1440)
        logger.info("🎵 Song Check posted")

    @commands.command(name="songtest")
    @commands.has_permissions(administrator=True)
    async def songtest(self, ctx):
        await self.post()


async def setup(bot, channel_id, post_day=4, post_hour=20, timezone="Europe/Oslo"):
    await bot.add_cog(SongCog(bot, channel_id, post_day, post_hour, timezone))
