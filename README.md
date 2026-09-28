# 🎮 PUBG + Birthday Discord Bot

A self-hosted Discord bot with two features in one process, loaded as separate Cogs:

- **PUBG Tracker** — automatically tracks matches for a list of players and posts rich stat embeds to your server
- **Birthday Bot** — tracks birthdays, announces them daily, gives a special role, and collects wishes

Both share one Discord connection (one bot token, one `!` command prefix) and the same config file, started together by `Main.py`.

---

## Features

### PUBG Tracker
- Auto match tracking — polls the PUBG API every 2.5 minutes across all tracked players
- Rich match embeds — kills, damage, headshots, longest kill, survival time, heals, boosts, revives, placement
- Group match detection — if multiple tracked players were in the same match, posts one combined embed
- Chicken dinner alert — special gold embed when any tracked player places #1, with optional role ping
- Weekly summaries — once a week (configurable day/hour/timezone, default Sunday 18:00 Europe/Oslo) posts best player, full leaderboard, and all-time longest kills
- Wall of Shame — posted right after the weekly summary, same run, same channel: the week's worst plays, followed by a dry, deadpan digest of every shame-worthy event from that week
- Cross-clan rivalries — scans match telemetry for kills between tracked players and players in other PUBG clans; `!rivalry` shows the top rival clans (kills vs deaths), and the weekly run posts the week's top 5 after the Wall of Shame (skipped if there were no cross-clan kills). Runs in the background, throttled so it never eats into the tracker's API rate limit
- SQLite storage — all match history and deduplication backed by a proper database
- Dynamic player management — add/remove players via Discord commands without restarting
- No duplicate posts — match IDs are persisted so restarts never double-post

### Birthday Bot
- Daily birthday announcements — checks every day at a configured hour and posts a birthday embed
- Birthday role — gives the person a special role for the whole day, removes it at midnight automatically
- Random birthday messages — never the same message twice (built to swap in AI-generated messages later)
- Wish system — members use `!wish @user` to send wishes that appear on the birthday embed
- Upcoming birthdays list — `!birthdays` shows everyone sorted by next occurrence
- PUBG crossover — if a tracked player gets a chicken dinner on their birthday, posts a special combined embed 🎂🍗

### Ask
- `!ask <question>` — answered by a local LLM via [Ollama](https://ollama.com) with a sarcastic, roasting persona (max 2 sentences, same language as the question)
- Model is unloaded from VRAM after `ask_keep_alive` of inactivity, so the GPU frees up between questions
- If Ollama is down, the bot replies that its brain is offline instead of erroring

---

## Project Structure

```
pubg-bot/
├── Main.py                  # Single entry point — creates the bot, loads both cogs, runs it
├── config.py                # Shared config.json + .env loader
├── bot.py                   # PUBGCog — Discord commands, polling loop, match posting
├── tracker.py               # Async PUBG API client
├── embeds.py                # Match embed builder + chicken dinner embed
├── weekly_stats.py          # Weekly stats calculations and embed builders
├── shame.py                 # Wall of Shame — weekly award board + weekly digest lines
├── rivalry.py               # Cross-clan rivalry scanner — telemetry kills, clan lookups, rate-limit throttling
├── database.py              # Async SQLite layer (matches, posted match IDs, bot state)
├── birthday_bot.py          # BirthdayCog — birthday commands, daily announcement, PUBG crossover
├── ask_bot.py               # AskCog — !ask, answered by a local Ollama model
├── fetch_longest_kills.py   # Weekly job to seed all-time longest kills data from the PUBG API
├── scripts/
│   └── clan_kill_scanner.py # Dry-run rivalry scan of one match with a skip-reason breakdown (debug, saves nothing)
├── players.txt              # PUBG player names to track (one per line)
├── config.example.json      # Config template — copy to config.json and fill in
└── .env.example             # Secrets template — copy to .env and fill in
```

---

## Requirements

- Python 3.9+
- A [PUBG Developer API key](https://developer.pubg.com/)
- A Discord bot token

```bash
pip install -r requirements.txt
```

Dependencies: `discord.py`, `aiohttp`, `aiosqlite`, `python-dotenv`

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/human1zer/pubg-bot.git
cd pubg-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure secrets

Copy the example files and fill in your values:

```bash
cp .env.example .env
cp config.example.json config.json
```

**`.env`** — secrets (never committed):
```
PUBG_API_KEY=your_pubg_api_key
DISCORD_TOKEN=your_discord_bot_token
```

**`config.json`** — everything else:
```json
{
  "pubg_api_key": "fallback_if_no_env",
  "discord_token": "fallback_if_no_env",
  "discord_channel_id": 0,
  "weekly_channel_id": 0,
  "shame_dry_run": false,
  "shame_top_n": 5,
  "weekly_post_day": 6,
  "weekly_post_hour": 18,
  "weekly_post_timezone": "Europe/Oslo",
  "check_interval_seconds": 150,
  "request_delay": 7,
  "max_retries": 3,
  "rivalry_enabled": true,
  "rivalry_reserve_requests": 4,
  "rivalry_cache_days": 7,
  "winner_role_id": 0,
  "posted_matches_max_history": 500,
  "birthday_channel_id": 0,
  "pubg_channel_id": 0,
  "birthday_announce_hour_utc": 8,
  "birthday_role_name": "🎂 Birthday"
}
```

| Key | Description |
|---|---|
| `discord_channel_id` | Channel for PUBG match posts |
| `weekly_channel_id` | Channel for weekly summaries, the Wall of Shame weekly post — award board + digest lines (`!shame`/`!shamenow`/`!shametest` always post here) — and the weekly clan rivalries section |
| `shame_dry_run` | When true, `!shametest` previews the weekly shame post without posting it to the weekly channel |
| `shame_top_n` | How many players to list per Wall of Shame award, worst first (default 5) |
| `weekly_post_day` | Day of week for the weekly summary + Wall of Shame run — `datetime.weekday()` values, Monday=0 … Sunday=6 (default 6) |
| `weekly_post_hour` | Local hour (in `weekly_post_timezone`) for the weekly summary + Wall of Shame run (default 18) |
| `weekly_post_timezone` | IANA timezone name for `weekly_post_hour` — using a local zone instead of UTC keeps the wall-clock hour fixed across daylight saving changes (default `Europe/Oslo`) |
| `rivalry_enabled` | Scan NORMAL/RANKED match telemetry for cross-clan kills involving tracked players (default true) |
| `rivalry_reserve_requests` | API requests the rivalry scanner always leaves unused in the current rate-limit window, so the tracker is never starved (default 4). The scanner also pauses entirely while a tracker cycle is running |
| `rivalry_cache_days` | How long player → clan and clan tag lookups are cached before being refreshed (default 7) |
| `winner_role_id` | Role ID to ping on chicken dinner (0 = disabled) |
| `posted_matches_max_history` | How many match IDs to keep for deduplication |
| `birthday_channel_id` | Channel for birthday announcements |
| `pubg_channel_id` | Channel for the birthday chicken dinner crossover post |
| `birthday_announce_hour_utc` | Hour (UTC) to post birthday announcements daily |
| `birthday_role_name` | Must match the role name exactly in your Discord server |
| `ask_ollama_url` | Ollama chat endpoint for `!ask` (default `http://localhost:11434/api/chat`) |
| `ask_model` | Ollama model for `!ask` — must already be pulled, e.g. `ollama pull llama3.2:3b` (default `llama3.2:3b`) |
| `ask_keep_alive` | How long Ollama keeps the model loaded in VRAM after a question (default `5m`) |
| `ask_system_prompt` | Persona / instructions for `!ask` answers |

### 3. Discord bot setup

1. Go to [https://discord.com/developers/applications](https://discord.com/developers/applications)
2. Create a new application and add a Bot
3. Copy the bot token into `.env`
4. Under **Privileged Gateway Intents**, enable:
   - **Server Members Intent** (required for birthday role)
   - **Message Content Intent**
5. Invite the bot with `bot` scope + `Send Messages`, `Embed Links`, `Manage Roles` permissions

### 4. Add players to `players.txt`

One player name per line. Lines starting with `#` are ignored:

```
# My squad
PlayerOne
PlayerTwo
PlayerThree
```

### 5. Create the birthday role in Discord

Go to **Server Settings → Roles → Create Role**, name it exactly `🎂 Birthday` (including the emoji). Give it a colour you like.

### 6. (Optional) Seed all-time longest kills

```bash
python3 fetch_longest_kills.py
```

This populates `longest_kills_alltime.json` which powers the third embed in the weekly summary.

---

## Running as a systemd service

PUBG tracking and the birthday bot now run in the same process (`Main.py` loads both
as Cogs on one Discord connection), so only one service is needed.

```bash
sudo nano /etc/systemd/system/pubgbot.service
```

```ini
[Unit]
Description=PUBG + Birthday Discord Bot
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/pubg-bot
ExecStart=/path/to/pubg-bot/venv/bin/python Main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Longest kills fetcher (weekly, Wednesdays 15:00)

```bash
sudo nano /etc/systemd/system/pubg-scraper.service
```

```ini
[Unit]
Description=PUBG Longest Kills Fetcher

[Service]
Type=oneshot
User=YOUR_USER
WorkingDirectory=/path/to/pubg-bot
ExecStart=/path/to/pubg-bot/venv/bin/python fetch_longest_kills.py
```

```bash
sudo nano /etc/systemd/system/pubg-scraper.timer
```

```ini
[Unit]
Description=Run PUBG scraper every Wednesday at 15:00

[Timer]
OnCalendar=Wed *-*-* 15:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

### Enable everything

```bash
sudo systemctl daemon-reload
sudo systemctl enable pubgbot birthdaybot pubg-scraper.timer
sudo systemctl start pubgbot birthdaybot pubg-scraper.timer
```

---

## PUBG Bot Commands

| Command | Who | Description |
|---|---|---|
| `!addplayer <name>` | Admin | Add a player to the tracking list |
| `!removeplayer <name>` | Admin | Remove a player from tracking |
| `!listplayers` | Anyone | Show all currently tracked players |
| `!best` | Anyone | All-time personal best records per player |
| `!rivalry [days]` | Anyone | Top 10 rival clans — tracked players' kills vs deaths against each clan (all time, or last N days) |
| `!weeklynow` | Admin | Manually trigger the weekly summary |
| `!shame` | Admin | Post the Wall of Shame award board + digest lines — always posts to the weekly channel |
| `!shamenow` | Admin | Force-post the Wall of Shame award board + digest lines |
| `!shametest` | Admin | Preview the full weekly post (award board + digest lines); respects `shame_dry_run` |
| `!testpost [name]` | Admin | Generate a test embed saved to `test_embed.txt` |

---


## Birthday Bot Commands

| Command | Who | Description |
|---|---|---|
| `!setbirthday DD/MM` | Anyone | Register your birthday |
| `!setbirthday DD/MM/YYYY` | Anyone | Register with birth year (shows age) |
| `!setbirthday @user DD/MM` | Admin | Set someone else's birthday |
| `!removebirthday` | Anyone | Remove your birthday |
| `!birthday @user` | Anyone | Check when someone's birthday is |
| `!birthdays` | Anyone | Full upcoming birthday list |
| `!nextbirthday` | Anyone | Who's birthday is next and how many days |
| `!wish @user <message>` | Anyone | Send a birthday wish |
| `!birthdaytest` | Admin | Preview the birthday embed for yourself |
| `!birthdayforce @user` | Admin | Force full announcement for any user right now |
| `!giverole @user` | Admin | Give the birthday role manually (testing) |
| `!removerole @user` | Admin | Remove the birthday role manually |

---

## Ask Command

| Command | Who | Description |
|---|---|---|
| `!ask <question>` | Anyone | Get a sarcastic answer from the local LLM (20s cooldown per user) |

---

## Weekly Summary

Every week, at the time set by `weekly_post_day` / `weekly_post_hour` / `weekly_post_timezone` (default **Sunday at 18:00 Europe/Oslo** — a local zone rather than UTC, so the hour stays 18:00 through daylight saving changes), the bot automatically posts three embeds to `weekly_channel_id`:

1. **Best Player of the Week** — top performer across kills, damage, wins, survival
2. **Leaderboard** — top 5 players ranked by composite score
3. **All-Time Longest Kills** — requires running `fetch_longest_kills.py` first

Right after, in the same run, it posts the Wall of Shame (see below) to the same `weekly_channel_id`.

Trigger manually anytime with `!weeklynow`. Casual, Arcade, and Airoyale matches are excluded from all stats.

---

## Wall of Shame

Every week, right after the weekly summary (same `weekly_post_day` / `weekly_post_hour` / `weekly_post_timezone` schedule, default **Sunday at 18:00 Europe/Oslo**), the bot posts to `weekly_channel_id`, covering the last 7 days: the award board, followed by the week's digest lines, both in the same channel.

**Award board:** each award is a ranked, numbered list of up to `shame_top_n` players (worst first), e.g.:

```
Suicide King
1. Flacketts — 4
2. Arma3Hunter — 3
3. CharlotteJB — 2
4. Hasibfit — 2
5. Squidddy — 1
```

- **Suicide King** — most self-inflicted deaths
- **Teamkiller** — most teammates killed
- **Roadkiller** — most roadkills
- **Blue Zone Food** — most deaths to the blue zone
- **Quitter** — most logouts mid-match
- **Worst KD** — lowest kills-per-death, minimum 10 matches played

Only players with a value greater than 0 are listed. An award is skipped entirely if nobody qualifies. Ties are broken alphabetically. Player names are compared case-insensitively (so `players.txt` entries like `Hasibfit` and `hasibfit` count as one player). The embed footer shows the total matches the board was calculated from, plus the date.

**Digest lines:** a dry, deadpan line for every shame-worthy event that week (suicides, teamkills, roadkills, blue zone deaths, logouts), most recent first — capped at 25 lines with a `+N more` if there's more. Since it runs once a week over a fixed 7-day window, there's no dedup — every qualifying event in range is included every time.

Trigger the award board manually anytime with `!shame` or `!shamenow` (both admin only) — both always post to `weekly_channel_id`, regardless of where the command was typed.

Preview the full weekly post (award board + digest lines) with `!shametest` (admin). With `shame_dry_run: true` in config, it logs the rendered output to stdout and echoes it back in the invoking channel instead of posting to `weekly_channel_id` — handy for tuning wording without spamming the real channel.

---

## Useful Commands

```bash
# Live logs
journalctl -u pubgbot -f
journalctl -u birthdaybot -f

# Restart after code changes
sudo systemctl restart pubgbot
sudo systemctl restart birthdaybot

# Check scraper timer
systemctl list-timers pubg-scraper.timer
```

---

## Notes

- All players are tracked on the **Steam** platform
- The PUBG bot and birthday bot run independently — either can be stopped without affecting the other
- Birthday data is stored in `birthdays.db`, PUBG data in `pubg_bot.db` — both excluded from git
- The birthday message system is designed to be swapped for AI-generated messages — see the comment inside `get_birthday_message()` in `birthday_bot.py`
