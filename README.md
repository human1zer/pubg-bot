# 🎮 PUBG + Birthday Discord Bot

A self-hosted Discord bot for a PUBG clan server. Everything runs in one process, loaded as separate Cogs:

- **PUBG Tracker** — automatically tracks matches for a list of players and posts telemetry match cards (map + stats image) to your server
- **Birthday Bot** — tracks birthdays, announces them daily, gives a special role, and collects wishes
- **Song Check** — weekly "drop the last song you listened to" post with its own thread
- **Ask** — `!ask`, mentions and replies answered by a local LLM ([Ollama](https://ollama.com)) that talks like one of the group and learns the group's lore nightly

All cogs share one Discord connection (one bot token, one `!` command prefix) and the same config file, started together by `Main.py`.

---

## Features

### PUBG Tracker
- Auto match tracking — polls the PUBG API every 2.5 minutes across all tracked players
- Telemetry match cards — for every team with a tracked player, renders a PNG card from the match telemetry:
  - header with placement (`#N / teams` or *WINNER WINNER CHICKEN DINNER!*), map, mode, time, duration, team kills and damage
  - a one-line roast of the squad from the local Ollama model (same persona and lore as `!ask`; skipped if Ollama is down)
  - the map zoomed on the team's movement — per-player paths (dashed in vehicles), kills, deaths and the zone — plus a **Final Fight** inset when the team's last two minutes happened in a small area
  - a stats table per player: kills, damage, knocks, assists, revives, survival time
- Embed fallback — if a card can't be rendered (telemetry missing, unknown map, timeout), the classic rich stat embed is posted instead; group matches with several tracked players are combined into one embed
- Chicken dinner alert — the winning team's card (or a gold winner embed) is also posted to `winner_channel_id`, with optional role ping
- Weekly summaries — once a week (configurable day/hour/timezone, default Sunday 18:00 Europe/Oslo) posts best player, full leaderboard, and all-time longest kills
- Wall of Shame — posted right after the weekly summary, same run, same channel: the week's worst plays, followed by a dry, deadpan digest of every shame-worthy event from that week
- Cross-clan rivalries — scans match telemetry for kills between tracked players and players in other PUBG clans; `!rivalry` shows the top rival clans (kills vs deaths), and the weekly run posts the week's top 5 after the Wall of Shame (skipped if there were no cross-clan kills). Runs in the background, throttled so it never eats into the tracker's API rate limit
- SQLite storage — all match history and deduplication backed by a proper database
- Dynamic player management — add/remove players via Discord commands without restarting
- No duplicate posts — match IDs are persisted so restarts never double-post
- Link filter — messages containing Instagram/Facebook links are deleted with a short notice

### Birthday Bot
- Daily birthday announcements — checks every day at a configured hour and posts a birthday embed
- Birthday role — gives the person a special role for the whole day, removes it at midnight automatically
- Random birthday messages — never the same message twice (built to swap in AI-generated messages later)
- Wish system — members use `!wish @user` to send wishes that appear on the birthday embed
- Upcoming birthdays list — `!birthdays` shows everyone sorted by next occurrence
- PUBG crossover — if a tracked player gets a chicken dinner on their birthday, posts a special combined embed 🎂🍗

### Song Check
- Every week (default Friday 20:00, in `weekly_post_timezone`) posts a "Song Check" message to `song_channel_id` pinging `@everyone`, and opens a "🎵 Last Song" thread for the replies
- Disabled when `song_channel_id` is 0

### Ask
- `!ask <question>` — answered by a local LLM via [Ollama](https://ollama.com), talking like one of the group (short, sarcastic, playful roasting, same language as the message)
- Also answers when someone @mentions the bot or replies to one of its messages — same 20s per-user cooldown
- Each answer sees the channel's last 20 messages plus the group lore
- Group lore — every night the bot reads the last 24h from `lore_channel_ids` (skipping bots and commands), has the model extract short notes (nicknames, recurring topics, inside jokes, running gags) and merges them into `lore.md`: deduped, capped at `lore_max_tokens`, notes not seen for `lore_stale_days` are dropped. Raw messages are never stored; `lore.md` is gitignored
- Model is unloaded from VRAM after `ask_keep_alive` of inactivity, so the GPU frees up between questions
- If Ollama is down, the bot replies that its brain is offline instead of erroring

---

## Project Structure

```
pubg-bot/
├── Main.py                  # Single entry point — creates the bot, loads all cogs, runs it
├── config.py                # Shared config.json + .env loader
├── bot.py                   # PUBGCog — Discord commands, polling loop, match posting
├── tracker.py               # Async PUBG API client
├── embeds.py                # Match embed builder + chicken dinner embed
├── weekly_stats.py          # Weekly stats calculations and embed builders
├── shame.py                 # Wall of Shame — weekly award board + weekly digest lines
├── rivalry.py               # Cross-clan rivalry scanner — telemetry kills, clan lookups, rate-limit throttling
├── database.py              # Async SQLite layer (matches, posted match IDs, bot state)
├── birthday_bot.py          # BirthdayCog — birthday commands, daily announcement, PUBG crossover
├── song_bot.py              # SongCog — weekly Song Check post + thread
├── ask_bot.py               # AskCog — !ask / mentions / replies, answered by a local Ollama model; nightly lore job
├── lore.py                  # Group lore — note extraction, merging, pruning, lore.md read/write
├── fetch_longest_kills.py   # Weekly job to seed all-time longest kills data from the PUBG API
├── scripts/
│   ├── clan_kill_scanner.py # Dry-run rivalry scan of one match with a skip-reason breakdown (debug, saves nothing)
│   └── match_card.py        # Telemetry match card renderer (map, paths, final-fight inset, roast, stats table)
├── shame.sql                # Ad-hoc sqlite3 queries for checking shame awards by hand
├── players.txt              # PUBG player names to track (one per line)
├── config.example.json      # Config template — copy to config.json and fill in
└── .env.example             # Secrets template — copy to .env and fill in
```

---

## Requirements

- Python 3.9+
- A [PUBG Developer API key](https://developer.pubg.com/)
- A Discord bot token
- DejaVu fonts (`fonts-dejavu-core` on Debian/Ubuntu) for the match cards
- Optional: [Ollama](https://ollama.com) running locally for `!ask`, lore and the match card roast line

```bash
pip install -r requirements.txt
pip install pillow   # match cards
```

Dependencies: `discord.py`, `aiohttp`, `aiosqlite`, `python-dotenv`, `Pillow`

Map images are downloaded once from the official [pubg/api-assets](https://github.com/pubg/api-assets) repo and cached in `scripts/.cache/maps/`. Match and telemetry JSON are deleted after each render, and rendered cards older than 2 days are cleaned up automatically.

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
  "pubg_api_key": "YOUR_PUBG_API_KEY_HERE",
  "discord_token": "YOUR_DISCORD_BOT_TOKEN_HERE",
  "discord_channel_id": 0,
  "weekly_channel_id": 0,
  "winner_channel_id": 0,
  "shame_dry_run": false,
  "shame_top_n": 5,
  "weekly_post_day": 6,
  "weekly_post_hour": 18,
  "weekly_post_timezone": "Europe/Oslo",
  "check_interval_seconds": 150,
  "request_delay": 9.0,
  "max_retries": 2,
  "rivalry_enabled": true,
  "rivalry_reserve_requests": 4,
  "rivalry_cache_days": 7,
  "winner_role_id": 0,
  "posted_matches_max_history": 500,
  "birthday_channel_id": 0,
  "birthday_announce_hour_utc": 8,
  "birthday_role_name": "🎂 Birthday",
  "pubg_channel_id": 0,
  "song_channel_id": 0,
  "song_post_day": 4,
  "song_post_hour": 20,
  "ask_ollama_url": "http://localhost:11434/api/chat",
  "ask_model": "llama3.2:3b",
  "ask_keep_alive": "5m",
  "ask_system_prompt": "You're a longtime member of this PUBG clan's Discord… (see config.example.json)",
  "ask_guild_id": 0,
  "lore_channel_ids": [],
  "lore_hour": 4,
  "lore_stale_days": 30,
  "lore_max_tokens": 1500,
  "lore_model": ""
}
```

| Key | Description |
|---|---|
| `discord_channel_id` | Channel for PUBG match posts (match cards / embeds) |
| `winner_channel_id` | Channel that also gets the winning team's card on a chicken dinner (0 = `discord_channel_id`) |
| `weekly_channel_id` | Channel for weekly summaries, the Wall of Shame weekly post — award board + digest lines (`!shame`/`!shamenow`/`!shametest` always post here) — and the weekly clan rivalries section |
| `shame_dry_run` | When true, `!shametest` previews the weekly shame post without posting it to the weekly channel |
| `shame_top_n` | How many players to list per Wall of Shame award, worst first (default 5) |
| `weekly_post_day` | Day of week for the weekly summary + Wall of Shame run — `datetime.weekday()` values, Monday=0 … Sunday=6 (default 6) |
| `weekly_post_hour` | Local hour (in `weekly_post_timezone`) for the weekly summary + Wall of Shame run (default 18) |
| `check_interval_seconds` | How often to poll the PUBG API for new matches (default 150) |
| `request_delay` | Seconds between PUBG API requests — keep ≥ 6 to stay under the rate limit |
| `max_retries` | Retries per failed PUBG API request |
| `weekly_post_timezone` | IANA timezone name for `weekly_post_hour` (also used by Song Check and the nightly lore job) — using a local zone instead of UTC keeps the wall-clock hour fixed across daylight saving changes (default `Europe/Oslo`) |
| `rivalry_enabled` | Scan NORMAL/RANKED match telemetry for cross-clan kills involving tracked players (default true) |
| `rivalry_reserve_requests` | API requests the rivalry scanner always leaves unused in the current rate-limit window, so the tracker is never starved (default 4). The scanner also pauses entirely while a tracker cycle is running |
| `rivalry_cache_days` | How long player → clan and clan tag lookups are cached before being refreshed (default 7) |
| `winner_role_id` | Role ID to ping on chicken dinner (0 = disabled) |
| `posted_matches_max_history` | How many match IDs to keep for deduplication |
| `birthday_channel_id` | Channel for birthday announcements (0 = birthday bot disabled) |
| `pubg_channel_id` | Channel for the birthday chicken dinner crossover post |
| `birthday_announce_hour_utc` | Hour (UTC) to post birthday announcements daily |
| `birthday_role_name` | Must match the role name exactly in your Discord server |
| `song_channel_id` | Channel for the weekly Song Check (0 = disabled) |
| `song_post_day` | Day of week for Song Check, Monday=0 … Sunday=6 (default 4 = Friday) |
| `song_post_hour` | Local hour (in `weekly_post_timezone`) for Song Check (default 20) |
| `ask_ollama_url` | Ollama chat endpoint for `!ask` (default `http://localhost:11434/api/chat`) |
| `ask_model` | Ollama model for `!ask` and the match card roast line — must already be pulled, e.g. `ollama pull llama3.2:3b` (default `llama3.2:3b`) |
| `ask_keep_alive` | How long Ollama keeps the model loaded in VRAM after a question (default `5m`) |
| `ask_system_prompt` | Persona / instructions for `!ask` answers |
| `ask_guild_id` | Only answer `!ask`, mentions and replies in this server (0 = any server) |
| `lore_channel_ids` | Channels the nightly lore job reads (empty = lore learning off) |
| `lore_hour` | Local hour (in `weekly_post_timezone`) for the nightly lore job (default 4) |
| `lore_stale_days` | Lore notes not seen in chat for this many days are dropped (default 30) |
| `lore_max_tokens` | Size cap for the lore injected into prompts — oldest notes go first (default 1500) |
| `lore_model` | Ollama model for the nightly lore job; empty = same as `ask_model`. A bigger model gives better notes and can be slow, it runs at night |

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

All features run in the same process (`Main.py` loads every Cog on one Discord
connection), so only one service is needed.

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
sudo systemctl enable pubgbot pubg-scraper.timer
sudo systemctl start pubgbot pubg-scraper.timer
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
| `!card [match_id]` | Admin | Render and post the telemetry match card(s) here — latest NORMAL match from the last 13 days if no ID (PUBG keeps telemetry ~14 days) |
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

## Song Check Command

| Command | Who | Description |
|---|---|---|
| `!songtest` | Admin | Post the Song Check message + thread right now |

---

## Ask Command

| Command | Who | Description |
|---|---|---|
| `!ask <question>` | Anyone | Ask the bot something — also works by @mentioning it or replying to it (20s cooldown per user) |
| `!lore` | Admin | DM you the current `lore.md` |
| `!lorenow` | Admin | Run the nightly lore update right now (last 24h) |
| `!loreclear` | Admin | Reset the lore (previous file kept as `lore.md.bak` on the server) |

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

# Restart after code changes
sudo systemctl restart pubgbot

# Check scraper timer
systemctl list-timers pubg-scraper.timer
```

---

## Notes

- All players are tracked on the **Steam** platform
- Birthday bot and Song Check are optional — leave `birthday_channel_id` / `song_channel_id` at 0 to disable them
- Birthday data is stored in `birthdays.db`, PUBG data in `pubg_bot.db` — both excluded from git
- Match cards can also be rendered from the shell: `venv/bin/python scripts/match_card.py <match_id> [player]` → `scripts/.cache/<match_id>-<rank>.png`
- The birthday message system is designed to be swapped for AI-generated messages — see the comment inside `get_birthday_message()` in `birthday_bot.py`
