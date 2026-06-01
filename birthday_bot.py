"""
birthday_bot.py — Discord Birthday Bot

Features:
  - Register birthdays (!setbirthday, !removebirthday)
  - Auto daily announcement at configured hour
  - Birthday role for the whole day (auto removed at midnight)
  - Random birthday messages (swap get_birthday_message() for AI later)
  - !birthdays — upcoming list
  - !nextbirthday — who's next
  - !wish @user — collect wishes, post together
  - !birthdayforce @user — admin: force announcement for any user
  - !giverole @user / !removerole @user — admin: test the birthday role
  - PUBG crossover — if birthday person gets chicken dinner, special embed
  - All data stored in SQLite

AI UPGRADE PATH:
  Replace get_birthday_message() body with an Anthropic API call.
  The function signature stays identical so nothing else needs changing.
"""

import asyncio
import logging
import os
import random
from datetime import datetime, date, timedelta, timezone
from typing import Optional

import aiosqlite
import discord
from discord.ext import commands, tasks

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "birthdays.db")

# ─────────────────────────────────────────────────────────────────────────────
# Birthday messages
# ─────────────────────────────────────────────────────────────────────────────

BIRTHDAY_MESSAGES = [
    "🎉 Oi {name}! Another year older, another year of absolutely sending it in PUBG. Happy birthday! 🍗",
    "🎂 Happy birthday {name}! May your loot be legendary and your enemies be bots today!",
    "🥳 {name} was born on this day and the world has been a more chaotic place ever since. Happy birthday! 🎂",
    "🎉 Today is {name}'s birthday! Go easy on them in the lobby... or don't. 🔫",
    "🎂 Happy birthday {name}! Wishing you more chicken dinners than birthday candles! 🍗",
    "🥳 Shoutout to {name} for surviving another year! Happy birthday legend! 🎉",
    "🎉 It's {name}'s birthday! Someone buy this person a drink and drop them hot! 🎂",
    "🎂 {name} has been alive for another full year. Incredible. Happy birthday! 🥳",
    "🎉 Happy birthday {name}! May your zones always be in your favour today! 🎂",
    "🥳 Big day for {name}! Born to frag, happy birthday! 🍗",
    "🎂 Celebrating {name} today! The real MVP, on and off the battlefield! 🎉",
    "🎉 {name}'s birthday! If birthdays were PUBG matches, you'd be dropping Pochinki. Respect. 🥳",
]


def get_birthday_message(name: str, age: Optional[int] = None) -> str:
    """
    Returns a random birthday message for the given name.

    ── AI UPGRADE ──────────────────────────────────────────────────────────
    To replace this with Claude-generated messages, swap the body with:

        import anthropic
        client = anthropic.Anthropic()
        prompt = f"Write a fun, short Discord birthday message for a PUBG player named {name}"
        if age:
            prompt += f" who is turning {age}"
        prompt += ". Keep it under 2 sentences, use 1-2 emojis, be playful."
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text

    The rest of the bot needs zero changes.
    ────────────────────────────────────────────────────────────────────────
    """
    msg = random.choice(BIRTHDAY_MESSAGES).format(name=name)
    if age:
        msg += f" 🎂 Turning **{age}** today!"
    return msg


# ─────────────────────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS birthdays (
                user_id     TEXT PRIMARY KEY,
                username    TEXT NOT NULL,
                day         INTEGER NOT NULL,
                month       INTEGER NOT NULL,
                year        INTEGER,
                timezone    TEXT DEFAULT 'UTC',
                added_at    TEXT NOT NULL
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wishes (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                birthday_user_id  TEXT NOT NULL,
                wisher_id         TEXT NOT NULL,
                wisher_name       TEXT NOT NULL,
                wish_text         TEXT,
                year              INTEGER NOT NULL,
                created_at        TEXT NOT NULL
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS announced (
                user_id  TEXT NOT NULL,
                year     INTEGER NOT NULL,
                PRIMARY KEY (user_id, year)
            );
        """)
        await db.commit()
    logger.info(f"✅ Birthday DB ready: {DB_PATH}")


async def set_birthday(user_id: str, username: str, day: int, month: int, year: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO birthdays (user_id, username, day, month, year, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                day=excluded.day,
                month=excluded.month,
                year=excluded.year,
                added_at=excluded.added_at;
        """, (user_id, username, day, month, year, datetime.now(timezone.utc).isoformat()))
        await db.commit()


async def remove_birthday(user_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM birthdays WHERE user_id = ?;", (user_id,))
        await db.commit()


async def get_all_birthdays():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM birthdays ORDER BY month, day;")
        return [dict(r) for r in await cursor.fetchall()]


async def get_todays_birthdays():
    today = date.today()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM birthdays WHERE day = ? AND month = ?;",
            (today.day, today.month)
        )
        return [dict(r) for r in await cursor.fetchall()]


async def already_announced(user_id: str, year: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM announced WHERE user_id = ? AND year = ?;",
            (user_id, year)
        )
        return await cursor.fetchone() is not None


async def mark_announced(user_id: str, year: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO announced (user_id, year) VALUES (?, ?);",
            (user_id, year)
        )
        await db.commit()


async def add_wish(birthday_user_id: str, wisher_id: str, wisher_name: str, wish_text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO wishes (birthday_user_id, wisher_id, wisher_name, wish_text, year, created_at)
            VALUES (?, ?, ?, ?, ?, ?);
        """, (birthday_user_id, wisher_id, wisher_name, wish_text,
              date.today().year, datetime.now(timezone.utc).isoformat()))
        await db.commit()


async def get_wishes_today(birthday_user_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM wishes WHERE birthday_user_id = ? AND year = ?;",
            (birthday_user_id, date.today().year)
        )
        return [dict(r) for r in await cursor.fetchall()]


# ─────────────────────────────────────────────────────────────────────────────
# Embeds
# ─────────────────────────────────────────────────────────────────────────────

def make_birthday_embed(member: discord.Member, birthday: dict, wishes: list) -> discord.Embed:
    age = None
    if birthday.get("year"):
        age = date.today().year - birthday["year"]

    msg = get_birthday_message(member.display_name, age)

    embed = discord.Embed(
        title=f"🎂 Happy Birthday, {member.display_name}!",
        description=msg,
        color=discord.Color.from_rgb(255, 105, 180),
        timestamp=datetime.now(),
    )

    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)

    if age:
        embed.add_field(name="🎈 Age", value=f"**{age}** years young!", inline=True)

    embed.add_field(
        name="📅 Birthday",
        value=f"**{birthday['day']:02d}/{birthday['month']:02d}**",
        inline=True,
    )

    if wishes:
        wish_lines = [f"💬 **{w['wisher_name']}**: {w['wish_text']}" for w in wishes[:10]]
        embed.add_field(
            name=f"🥳 Wishes ({len(wishes)})",
            value="\n".join(wish_lines),
            inline=False,
        )

    embed.set_footer(text="Use !wish @user to send a birthday wish!")
    return embed


def make_pubg_birthday_dinner_embed(member: discord.Member, match: dict) -> discord.Embed:
    embed = discord.Embed(
        title="🎂🍗 BIRTHDAY CHICKEN DINNER! 🍗🎂",
        description=(
            f"**{member.display_name}** got a chicken dinner ON THEIR BIRTHDAY!\n"
            f"This is absolutely unhinged. Happy birthday legend! 🥳"
        ),
        color=discord.Color.from_rgb(255, 215, 0),
        timestamp=datetime.now(),
    )
    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)

    stats = match.get("all_players_stats", {}).get(member.display_name, {})
    if stats:
        embed.add_field(
            name="🏆 Birthday Dinner Stats",
            value=(
                f"💀 **{stats.get('kills', 0)}** kills\n"
                f"💥 **{stats.get('damage_dealt', 0):,.0f}** damage\n"
                f"🔭 **{stats.get('longest_kill', 0):.0f}m** longest kill"
            ),
            inline=False,
        )
    return embed


def make_upcoming_embed(birthdays: list) -> discord.Embed:
    today = date.today()
    embed = discord.Embed(
        title="📅 Upcoming Birthdays",
        color=discord.Color.from_rgb(255, 165, 0),
        timestamp=datetime.now(),
    )

    upcoming = []
    for b in birthdays:
        bday = date(today.year, b["month"], b["day"])
        if bday < today:
            bday = date(today.year + 1, b["month"], b["day"])
        days_away = (bday - today).days
        upcoming.append((days_away, b))

    upcoming.sort(key=lambda x: x[0])

    if not upcoming:
        embed.description = "No birthdays registered yet!"
        return embed

    lines = []
    for days_away, b in upcoming[:15]:
        if days_away == 0:
            label = "🎂 **TODAY!**"
        elif days_away == 1:
            label = "🎉 Tomorrow"
        else:
            label = f"📅 In **{days_away}** days"
        lines.append(f"**{b['username']}** — {b['day']:02d}/{b['month']:02d} {label}")

    embed.description = "\n".join(lines)
    embed.set_footer(text=f"{len(upcoming)} birthdays registered")
    return embed


# ─────────────────────────────────────────────────────────────────────────────
# Bot
# ─────────────────────────────────────────────────────────────────────────────

class BirthdayBot:
    def __init__(self, token: str, birthday_channel_id: int,
                 birthday_role_name: str = "🎂 Birthday",
                 announce_hour_utc: int = 8,
                 pubg_channel_id: int = 0):

        self.token               = token
        self.birthday_channel_id = birthday_channel_id
        self.birthday_role_name  = birthday_role_name
        self.announce_hour_utc   = announce_hour_utc
        self.pubg_channel_id     = pubg_channel_id

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        self.client = commands.Bot(command_prefix="!", intents=intents)
        self.client.event(self.on_ready)
        self._register_commands()

    # ─────────────────────────────────────────────────────────────────────────
    # Commands
    # ─────────────────────────────────────────────────────────────────────────

    def _register_commands(self):

        @self.client.command(name="setbirthday")
        async def setbirthday(ctx, target: str, date_str: str = None):
            """
            !setbirthday DD/MM         — set your own birthday
            !setbirthday DD/MM/YYYY    — with birth year (shows age)
            !setbirthday @user DD/MM   — admin sets someone else's
            """
            if ctx.message.mentions and date_str:
                if not ctx.author.guild_permissions.administrator:
                    await ctx.send("❌ Only admins can set other people's birthdays!")
                    return
                member = ctx.message.mentions[0]
                date_input = date_str
            else:
                member = ctx.author
                date_input = target

            parts = date_input.split("/")
            try:
                day   = int(parts[0])
                month = int(parts[1])
                year  = int(parts[2]) if len(parts) == 3 else None
                if not (1 <= day <= 31 and 1 <= month <= 12):
                    raise ValueError
                if year and not (1900 <= year <= date.today().year):
                    raise ValueError
            except (ValueError, IndexError):
                await ctx.send("❌ Invalid date! Use `DD/MM` or `DD/MM/YYYY` — e.g. `!setbirthday 25/12` or `!setbirthday 25/12/1995`")
                return

            await set_birthday(str(member.id), member.display_name, day, month, year)
            age_str = f" (born {year})" if year else ""
            await ctx.send(f"✅ Birthday set for **{member.display_name}**: **{day:02d}/{month:02d}**{age_str} 🎂")

        @self.client.command(name="removebirthday")
        async def removebirthday(ctx, target: discord.Member = None):
            """!removebirthday — remove your own birthday (admin can target @user)"""
            if target and not ctx.author.guild_permissions.administrator:
                await ctx.send("❌ Only admins can remove other people's birthdays!")
                return
            member = target or ctx.author
            await remove_birthday(str(member.id))
            await ctx.send(f"✅ Removed birthday for **{member.display_name}**.")

        @self.client.command(name="birthday")
        async def birthday(ctx, member: discord.Member = None):
            """!birthday @user — check when someone's birthday is"""
            member = member or ctx.author
            all_bdays = await get_all_birthdays()
            bday = next((b for b in all_bdays if b["user_id"] == str(member.id)), None)
            if not bday:
                await ctx.send(f"❌ No birthday registered for **{member.display_name}**.")
                return
            today = date.today()
            bday_this_year = date(today.year, bday["month"], bday["day"])
            if bday_this_year < today:
                bday_this_year = date(today.year + 1, bday["month"], bday["day"])
            days_away = (bday_this_year - today).days
            age_str = f" (turns **{today.year - bday['year']}**)" if bday.get("year") else ""
            if days_away == 0:
                await ctx.send(f"🎂 **{member.display_name}**'s birthday is **TODAY!** {age_str}")
            else:
                await ctx.send(
                    f"📅 **{member.display_name}**'s birthday: **{bday['day']:02d}/{bday['month']:02d}**{age_str} — in **{days_away}** days."
                )

        @self.client.command(name="birthdays")
        async def birthdays(ctx):
            """!birthdays — list all upcoming birthdays"""
            all_bdays = await get_all_birthdays()
            embed = make_upcoming_embed(all_bdays)
            await ctx.send(embed=embed)

        @self.client.command(name="nextbirthday")
        async def nextbirthday(ctx):
            """!nextbirthday — who's birthday is coming up next"""
            all_bdays = await get_all_birthdays()
            if not all_bdays:
                await ctx.send("❌ No birthdays registered yet!")
                return
            today = date.today()
            upcoming = []
            for b in all_bdays:
                bday = date(today.year, b["month"], b["day"])
                if bday < today:
                    bday = date(today.year + 1, b["month"], b["day"])
                days_away = (bday - today).days
                upcoming.append((days_away, b))
            upcoming.sort(key=lambda x: x[0])
            days_away, next_b = upcoming[0]
            if days_away == 0:
                await ctx.send(f"🎂 **{next_b['username']}**'s birthday is **TODAY!**")
            else:
                await ctx.send(
                    f"🎉 Next birthday: **{next_b['username']}** on "
                    f"**{next_b['day']:02d}/{next_b['month']:02d}** — in **{days_away}** days!"
                )

        @self.client.command(name="wish")
        async def wish(ctx, member: discord.Member, *, message: str = "Happy birthday! 🎉"):
            """!wish @user <message> — send a birthday wish"""
            if member.id == ctx.author.id:
                await ctx.send("❌ You can't wish yourself happy birthday! 😄")
                return
            all_bdays = await get_all_birthdays()
            bday = next((b for b in all_bdays if b["user_id"] == str(member.id)), None)
            if not bday:
                await ctx.send(f"❌ **{member.display_name}** hasn't registered a birthday!")
                return
            today = date.today()
            if not (bday["day"] == today.day and bday["month"] == today.month):
                await ctx.send(f"❌ It's not **{member.display_name}**'s birthday today!")
                return
            await add_wish(str(member.id), str(ctx.author.id), ctx.author.display_name, message)
            await ctx.send(f"✅ Wish sent to **{member.display_name}**! 🎂")
            channel = self.client.get_channel(self.birthday_channel_id)
            if channel:
                wishes = await get_wishes_today(str(member.id))
                updated_embed = make_birthday_embed(member, bday, wishes)
                await channel.send(
                    f"💬 **{ctx.author.display_name}** just wished **{member.display_name}** happy birthday!",
                    embed=updated_embed,
                )

        @self.client.command(name="birthdaytest")
        async def birthdaytest(ctx):
            """!birthdaytest — admin: preview birthday embed for yourself"""
            if not ctx.author.guild_permissions.administrator:
                await ctx.send("❌ Admins only!")
                return
            fake_bday = {
                "user_id": str(ctx.author.id),
                "username": ctx.author.display_name,
                "day": date.today().day,
                "month": date.today().month,
                "year": 1990,
            }
            embed = make_birthday_embed(ctx.author, fake_bday, [])
            await ctx.send("🧪 Test birthday announcement:", embed=embed)

        @self.client.command(name="birthdayforce")
        async def birthdayforce(ctx, member: discord.Member):
            """!birthdayforce @user — admin: force full birthday announcement for any user"""
            if not ctx.author.guild_permissions.administrator:
                await ctx.send("❌ Admins only!")
                return
            all_bdays = await get_all_birthdays()
            bday = next((b for b in all_bdays if b["user_id"] == str(member.id)), None)
            if not bday:
                await ctx.send(f"❌ No birthday registered for **{member.display_name}**!")
                return
            await self._give_birthday_role(member)
            wishes = await get_wishes_today(str(member.id))
            embed = make_birthday_embed(member, bday, wishes)
            channel = self.client.get_channel(self.birthday_channel_id)
            if channel:
                await channel.send("@everyone", embed=embed)
                await mark_announced(str(member.id), datetime.now(timezone.utc).year)
                await ctx.send(f"✅ Birthday announced for **{member.display_name}**!")
            else:
                await ctx.send("❌ Birthday channel not found!")

        @self.client.command(name="giverole")
        async def giverole(ctx, member: discord.Member = None):
            """!giverole @user — admin: give birthday role for testing"""
            if not ctx.author.guild_permissions.administrator:
                await ctx.send("❌ Admins only!")
                return
            member = member or ctx.author
            await self._give_birthday_role(member)
            await ctx.send(f"✅ Birthday role given to **{member.display_name}**!")

        @self.client.command(name="removerole")
        async def removerole(ctx, member: discord.Member = None):
            """!removerole @user — admin: manually remove birthday role"""
            if not ctx.author.guild_permissions.administrator:
                await ctx.send("❌ Admins only!")
                return
            member = member or ctx.author
            role = discord.utils.get(member.guild.roles, name=self.birthday_role_name)
            if not role:
                await ctx.send(f"❌ Role '{self.birthday_role_name}' not found!")
                return
            await member.remove_roles(role)
            await ctx.send(f"✅ Birthday role removed from **{member.display_name}**!")

    # ─────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    async def on_ready(self):
        logger.info(f"✅ Birthday bot connected as {self.client.user}")
        self.daily_birthday_check.start()
        self.midnight_role_cleanup.start()

    # ─────────────────────────────────────────────────────────────────────────
    # Loops
    # ─────────────────────────────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def daily_birthday_check(self):
        now = datetime.now(timezone.utc)
        # Run from announce_hour onwards so missed birthdays are caught same day
        if now.hour < self.announce_hour_utc:
            return

        logger.info("🎂 Running daily birthday check...")
        todays = await get_todays_birthdays()
        if not todays:
            logger.info("No birthdays today.")
            return

        channel = self.client.get_channel(self.birthday_channel_id)
        if not channel:
            logger.error(f"❌ Birthday channel not found: {self.birthday_channel_id}")
            return

        for bday in todays:
            if await already_announced(bday["user_id"], now.year):
                continue

            member = None
            for guild in self.client.guilds:
                member = guild.get_member(int(bday["user_id"]))
                if member:
                    break

            if not member:
                logger.warning(f"⚠️ Member not found for user_id {bday['user_id']}")
                continue

            await self._give_birthday_role(member)
            wishes = await get_wishes_today(bday["user_id"])
            embed = make_birthday_embed(member, bday, wishes)
            await channel.send("@everyone", embed=embed)
            await mark_announced(bday["user_id"], now.year)
            logger.info(f"🎂 Posted birthday for {member.display_name}")

    @tasks.loop(hours=1)
    async def midnight_role_cleanup(self):
        """Remove birthday role from anyone whose birthday was yesterday."""
        now = datetime.now(timezone.utc)
        if now.hour != 0:
            return

        for guild in self.client.guilds:
            role = discord.utils.get(guild.roles, name=self.birthday_role_name)
            if not role:
                continue
            for member in guild.members:
                if role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Birthday over")
                        logger.info(f"🎂 Removed birthday role from {member.display_name}")
                    except Exception as e:
                        logger.warning(f"⚠️ Could not remove role from {member.display_name}: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _give_birthday_role(self, member: discord.Member):
        role = discord.utils.get(member.guild.roles, name=self.birthday_role_name)
        if not role:
            logger.warning(f"⚠️ Role '{self.birthday_role_name}' not found in {member.guild.name}")
            return
        try:
            await member.add_roles(role, reason="Birthday!")
            logger.info(f"🎂 Gave birthday role to {member.display_name}")
        except Exception as e:
            logger.warning(f"⚠️ Could not give birthday role: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # PUBG crossover
    # ─────────────────────────────────────────────────────────────────────────

    async def check_pubg_birthday_dinner(self, match: dict):
        """
        Call this from bot.py after posting a PUBG match embed.
        If a winner is having their birthday today, posts the special embed.
        """
        if not self.pubg_channel_id:
            return

        winners = [
            name for name, stats in match.get("all_players_stats", {}).items()
            if stats.get("rank") == 1
        ]
        if not winners:
            return

        todays = await get_todays_birthdays()
        today_names = {b["username"].lower() for b in todays}

        for winner in winners:
            if winner.lower() not in today_names:
                continue
            for guild in self.client.guilds:
                member = discord.utils.find(
                    lambda m: m.display_name.lower() == winner.lower(), guild.members
                )
                if member:
                    channel = self.client.get_channel(self.pubg_channel_id)
                    if channel:
                        embed = make_pubg_birthday_dinner_embed(member, match)
                        await channel.send(embed=embed)
                        logger.info(f"🎂🍗 Birthday dinner posted for {winner}!")
                    break

    def run(self):
        self.client.run(self.token)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

birthday_bot_instance: Optional[BirthdayBot] = None


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    import json
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(config_path) as f:
        config = json.load(f)

    token            = os.getenv("DISCORD_TOKEN") or config.get("discord_token")
    birthday_channel = config.get("birthday_channel_id", 0)
    pubg_channel     = config.get("pubg_channel_id", 0)
    announce_hour    = config.get("birthday_announce_hour_utc", 8)
    role_name        = config.get("birthday_role_name", "🎂 Birthday")

    if not token:
        print("❌ No Discord token found!")
        return
    if not birthday_channel:
        print("❌ Set birthday_channel_id in config.json!")
        return

    asyncio.run(init_db())

    global birthday_bot_instance
    birthday_bot_instance = BirthdayBot(
        token=token,
        birthday_channel_id=birthday_channel,
        birthday_role_name=role_name,
        announce_hour_utc=announce_hour,
        pubg_channel_id=pubg_channel,
    )
    birthday_bot_instance.run()


if __name__ == "__main__":
    main()