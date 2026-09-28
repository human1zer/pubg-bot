import asyncio
import logging

import aiohttp
import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

MAX_REPLY_CHARS = 400
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)


class AskCog(commands.Cog):
    def __init__(self, bot, ollama_url, model, system_prompt, keep_alive):
        self.bot = bot
        self.ollama_url = ollama_url
        self.model = model
        self.system_prompt = system_prompt
        self.keep_alive = keep_alive      # how long Ollama keeps the model in VRAM
        self.session = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    async def query_ollama(self, question):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": question},
            ],
            "stream": False,
            "keep_alive": self.keep_alive,
        }
        async with self.session.post(self.ollama_url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return data["message"]["content"].strip()

    @commands.command(name="ask")
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def ask(self, ctx, *, question: str):
        try:
            async with ctx.typing():
                answer = await self.query_ollama(question)
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as e:
            logger.warning(f"⚠️ !ask failed (Ollama at {self.ollama_url}): {e!r}")
            await ctx.reply("🤖 My brain is offline right now. Try again later.", mention_author=False)
            return

        if not answer:
            answer = "…"
        if len(answer) > MAX_REPLY_CHARS:
            answer = answer[:MAX_REPLY_CHARS - 1].rstrip() + "…"
        await ctx.reply(answer, allowed_mentions=discord.AllowedMentions.none())

    @ask.error
    async def ask_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            reply = f"⏳ Slow down. Try again in {error.retry_after:.0f}s."
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


async def setup(bot, ollama_url, model, system_prompt, keep_alive="5m"):
    await bot.add_cog(AskCog(bot, ollama_url, model, system_prompt, keep_alive))
