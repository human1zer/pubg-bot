import asyncio
import datetime
import json
import logging
import re
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands, tasks

import lore

logger = logging.getLogger(__name__)

MAX_REPLY_CHARS    = 400
MAX_QUESTION_CHARS = 1000
HISTORY_MESSAGES   = 20
HISTORY_TOKENS     = 1100   # budget for recent chat in the !ask prompt
HISTORY_LINE_CHARS = 300
ASK_PREDICT        = 200
ASK_COOLDOWN       = 20     # seconds per user, shared by !ask, mentions and replies
REQUEST_TIMEOUT    = aiohttp.ClientTimeout(total=60)
LORE_TIMEOUT       = aiohttp.ClientTimeout(total=300)   # extraction calls are slower
OFFLINE_REPLY      = "🤖 My brain is offline right now. Try again later."
# The model sometimes mimics the "Name: message" chat format in its answer
SPEAKER_LABEL_RE   = re.compile(r"^\s*[^\s:]{1,32}(?: \(you\))?:\s+")


class AskCog(commands.Cog):
    def __init__(self, bot, ollama_url, model, system_prompt, keep_alive, guild_id,
                 lore_channel_ids, lore_hour, timezone, lore_stale_days, lore_max_tokens, lore_model=None):
        self.bot = bot
        self.ollama_url = ollama_url
        self.model = model
        self.lore_model = lore_model or model   # nightly job can use a bigger, slower model
        self.system_prompt = system_prompt
        self.keep_alive = keep_alive      # how long Ollama keeps the model in VRAM
        self.guild_id = guild_id          # 0 = any server
        self.lore_channel_ids = lore_channel_ids
        self.lore_stale_days = lore_stale_days
        self.lore_max_tokens = lore_max_tokens
        self.tz = ZoneInfo(timezone)
        self.session = None
        self.lore_lock = asyncio.Lock()
        self.cooldown = commands.CooldownMapping.from_cooldown(1, ASK_COOLDOWN, commands.BucketType.user)
        self.nightly_lore.change_interval(time=datetime.time(hour=lore_hour, tzinfo=self.tz))

    async def cog_load(self):
        self.session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
        if self.lore_channel_ids:
            self.nightly_lore.start()
        else:
            logger.warning("⚠️ lore_channel_ids not set — nightly lore learning disabled.")

    async def cog_unload(self):
        self.nightly_lore.cancel()
        if self.session:
            await self.session.close()

    # ─────────────────────────────────────────────────────────────────────────
    # Ollama
    # ─────────────────────────────────────────────────────────────────────────

    async def ollama_chat(self, messages, schema=None, num_predict=ASK_PREDICT, timeout=None, model=None):
        """One chat call. Returns the reply text, or parsed JSON when `schema` is given."""
        payload = {
            "model": model or self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"num_ctx": lore.NUM_CTX, "num_predict": num_predict},
        }
        if schema:
            payload["format"] = schema
        async with self.session.post(self.ollama_url, json=payload, timeout=timeout or REQUEST_TIMEOUT) as resp:
            resp.raise_for_status()
            data = await resp.json()
        content = data["message"]["content"].strip()
        return json.loads(content) if schema else content

    async def lore_chat(self, messages, schema, num_predict):
        return await self.ollama_chat(messages, schema=schema, num_predict=num_predict,
                                      timeout=LORE_TIMEOUT, model=self.lore_model)

    # ─────────────────────────────────────────────────────────────────────────
    # Answering (!ask, @mentions, replies to the bot)
    # ─────────────────────────────────────────────────────────────────────────

    def in_scope(self, guild):
        if guild is None:
            return True
        return not self.guild_id or guild.id == self.guild_id

    def speaker(self, member):
        if member.id == self.bot.user.id:
            return f"{member.display_name} (you)"
        return member.display_name

    async def recent_chat(self, message):
        """Last HISTORY_MESSAGES before `message`, oldest first, trimmed to HISTORY_TOKENS."""
        lines, used = [], 0
        async for m in message.channel.history(limit=HISTORY_MESSAGES, before=message):
            if m.author.bot and m.author.id != self.bot.user.id:
                continue
            text = m.clean_content or ("[attachment]" if m.attachments else "")
            if not text:
                continue
            line = f"{self.speaker(m.author)}: {text[:HISTORY_LINE_CHARS]}"
            cost = lore.estimate_tokens(line)
            if used + cost > HISTORY_TOKENS:
                break
            lines.append(line)
            used += cost
        return "\n".join(reversed(lines))

    async def build_messages(self, message, question, replied_to=None):
        me = message.guild.me if message.guild else self.bot.user
        system = f"{self.system_prompt}\n\nYour name in this server is {me.display_name}."
        group_lore = lore.render_prompt(lore.load_notes())
        if group_lore:
            system += f"\n\nWhat you know about the group from past chats (may be outdated):\n{group_lore}"
        history = await self.recent_chat(message)
        if history:
            where = f"#{message.channel.name}" if message.guild else "this DM"
            system += f"\n\nRecent messages in {where}, oldest first:\n{history}"

        user = f"{message.author.display_name}: {question[:MAX_QUESTION_CHARS]}"
        if replied_to is not None:
            user = f"(replying to your message: \"{replied_to.clean_content[:HISTORY_LINE_CHARS]}\")\n{user}"
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    async def respond(self, message, question, replied_to=None):
        retry_after = self.cooldown.get_bucket(message).update_rate_limit()
        if retry_after:
            await message.reply(f"⏳ Slow down. Try again in {retry_after:.0f}s.", mention_author=False)
            return
        try:
            async with message.channel.typing():
                answer = await self.ollama_chat(await self.build_messages(message, question, replied_to))
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as e:
            logger.warning(f"⚠️ !ask failed (Ollama at {self.ollama_url}): {e!r}")
            await message.reply(OFFLINE_REPLY, mention_author=False)
            return

        answer = SPEAKER_LABEL_RE.sub("", answer, count=1)
        if not answer:
            answer = "…"
        if len(answer) > MAX_REPLY_CHARS:
            answer = answer[:MAX_REPLY_CHARS - 1].rstrip() + "…"
        await message.reply(answer, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="ask")
    @commands.guild_only()
    async def ask(self, ctx, *, question: str):
        if not self.in_scope(ctx.guild):
            return
        await self.respond(ctx.message, question)

    @ask.error
    async def ask_error(self, ctx, error):
        if isinstance(error, commands.NoPrivateMessage):
            reply = "❌ `!ask` only works in the server."
        elif isinstance(error, commands.MissingRequiredArgument):
            reply = "❌ Usage: `!ask <question>`"
        else:
            original = getattr(error, "original", error)
            logger.error(f"❌ !ask failed: {original!r}", exc_info=(type(original), original, original.__traceback__))
            reply = "❌ Something went wrong running `!ask`."
        try:
            await ctx.reply(reply, mention_author=False)
        except discord.HTTPException:
            pass

    async def replied_bot_message(self, message):
        """The bot's message that `message` replies to, or None."""
        ref = message.reference
        if ref is None or ref.message_id is None:
            return None
        target = ref.resolved
        if target is None:
            try:
                target = await message.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                return None
        if isinstance(target, discord.Message) and target.author.id == self.bot.user.id:
            return target
        return None

    def mention_to_question(self, message):
        text = re.sub(rf"<@!?{self.bot.user.id}>", "", message.content)
        for m in message.mentions:
            text = re.sub(rf"<@!?{m.id}>", f"@{m.display_name}", text)
        return " ".join(text.split())

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return
        is_dm = message.guild is None
        if not is_dm and not self.in_scope(message.guild):
            return
        if (await self.bot.get_context(message)).valid:
            return   # a command — the command handler deals with it
        replied_to = await self.replied_bot_message(message)
        if not is_dm and replied_to is None and self.bot.user not in message.mentions:
            return
        question = self.mention_to_question(message) or "(pinged you without saying anything)"
        try:
            await self.respond(message, question, replied_to)
        except discord.HTTPException as e:
            logger.warning(f"⚠️ Couldn't answer mention/reply: {e!r}")

    # ─────────────────────────────────────────────────────────────────────────
    # Nightly lore learning
    # ─────────────────────────────────────────────────────────────────────────

    @tasks.loop(time=datetime.time(hour=0))  # overridden in __init__
    async def nightly_lore(self):
        try:
            await self.run_lore_update()
        except Exception as e:
            logger.error(f"❌ Nightly lore update failed: {e!r}", exc_info=True)

    @nightly_lore.before_loop
    async def before_nightly_lore(self):
        await self.bot.wait_until_ready()

    async def collect_lines(self):
        """Last 24h of human, non-command messages from the lore channels. Memory only."""
        after = discord.utils.utcnow() - datetime.timedelta(hours=24)
        lines = []
        for channel_id in self.lore_channel_ids:
            try:
                channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            except discord.HTTPException as e:
                logger.warning(f"⚠️ Lore channel {channel_id} unavailable: {e!r}")
                continue
            channel_lines = []
            async for m in channel.history(after=after, limit=None, oldest_first=True):
                if m.author.bot or m.content.startswith(self.bot.command_prefix):
                    continue
                text = m.clean_content.strip()
                if not text:
                    continue
                name = m.author.display_name
                if m.author.name != name:
                    name = f"{name} ({m.author.name})"
                channel_lines.append(f"{name}: {text}")
            if channel_lines:
                lines.append(f"--- #{channel.name} ---")
                lines += channel_lines
        return lines

    async def run_lore_update(self):
        async with self.lore_lock:
            lines = await self.collect_lines()
            if not lines:
                logger.info("📜 Lore: no new messages in the last 24h")
                return None
            stats = await lore.update_lore(
                self.lore_chat, lines,
                today=datetime.datetime.now(self.tz).date(),
                stale_days=self.lore_stale_days,
                max_tokens=self.lore_max_tokens,
            )
            logger.info(f"📜 Lore updated: {stats}")
            return stats

    @commands.command(name="lore")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def lore_show(self, ctx):
        """DM the current lore file to the admin who asked."""
        if not lore.LORE_PATH.exists():
            await ctx.reply("📜 Lore is empty.", mention_author=False)
            return
        text = lore.LORE_PATH.read_text(encoding="utf-8")
        try:
            if len(text) <= 1900:
                await ctx.author.send(f"```md\n{text}\n```")
            else:
                await ctx.author.send(file=discord.File(lore.LORE_PATH, filename="lore.md"))
        except discord.Forbidden:
            await ctx.reply("❌ I can't DM you — allow DMs from server members.", mention_author=False)
            return
        await ctx.reply("📬 Sent you the lore in DM.", mention_author=False)

    @commands.command(name="loreclear")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def lore_clear(self, ctx):
        if self.lore_lock.locked():
            await ctx.reply("⏳ Lore update is running — try again in a minute.", mention_author=False)
            return
        if lore.LORE_PATH.exists():
            lore.LORE_PATH.replace(lore.LORE_PATH.with_suffix(".md.bak"))
        await ctx.reply("🧹 Lore cleared (previous version kept as `lore.md.bak` on the server).", mention_author=False)

    @commands.command(name="lorenow")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def lore_now(self, ctx):
        if not self.lore_channel_ids:
            await ctx.reply("❌ `lore_channel_ids` isn't set in config.json.", mention_author=False)
            return
        if self.lore_lock.locked():
            await ctx.reply("⏳ Lore update is already running.", mention_author=False)
            return
        await ctx.reply("📜 Updating lore from the last 24h — this takes a few minutes.", mention_author=False)
        try:
            stats = await self.run_lore_update()
        except Exception as e:
            logger.error(f"❌ !lorenow failed: {e!r}", exc_info=True)
            await ctx.reply(OFFLINE_REPLY if isinstance(e, (aiohttp.ClientError, RuntimeError)) else
                            "❌ Lore update failed — check the logs.", mention_author=False)
            return
        if stats is None:
            await ctx.reply("📜 No messages in the last 24h — nothing to learn.", mention_author=False)
        else:
            await ctx.reply(
                f"📜 Done: read {stats['messages']} messages, {stats['candidates']} candidate notes → "
                f"lore has {stats['after']} notes (~{stats['tokens']} tokens). `!lore` to see it.",
                mention_author=False,
            )


async def setup(bot, ollama_url, model, system_prompt, keep_alive="5m", guild_id=0,
                lore_channel_ids=(), lore_hour=4, timezone="Europe/Oslo",
                lore_stale_days=30, lore_max_tokens=1500, lore_model=None):
    await bot.add_cog(AskCog(
        bot, ollama_url, model, system_prompt, keep_alive, guild_id,
        list(lore_channel_ids), lore_hour, timezone, lore_stale_days, lore_max_tokens, lore_model,
    ))
