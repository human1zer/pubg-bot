# 🎮 PUBG + Birthday Discord Bot

A self-hosted Discord bot suite with two bots in one repo:

- **PUBG Tracker** — automatically tracks matches for a list of players and posts rich stat embeds to your server
- **Birthday Bot** — tracks birthdays, announces them daily, gives a special role, and collects wishes

Both run as systemd services and share the same config file.

---

## Features

### PUBG Tracker
- Auto match tracking — polls the PUBG API every 2.5 minutes across all tracked players
- Rich match embeds — kills, damage, headshots, longest kill, survival time, heals, boosts, revives, placement
- Group match detection — if multiple tracked players were in the same match, posts one combined embed
- Chicken dinner alert — special gold embed when any tracked player places #1, with optional role ping
- Weekly summaries — every Wednesday at 18:00 UTC posts best player, full leaderboard, and all-time longest kills
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

---

## Project Structure

```
pubg-bot/
├── Main.py                  # PUBG bot entry point
├── bot.py                   # PUBG bot — Discord commands, polling loop, match posting
├── tracker.py               # Async PUBG API client
├── embeds.py                # Match embed builder + chicken dinner embed
├── weekly_stats.py          # Weekly stats calculations and embed builders
├── database.py              # Async SQLite layer (matches + posted match IDs)
├── birthday_bot.py          # Birthday bot — full standalone bot
├── scrape_longest_kills.py  # One-time scraper to seed all-time longest kills data
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
  "check_interval_seconds": 150,
  "request_delay": 7,
  "max_retries": 3,
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
| `weekly_channel_id` | Channel for weekly summaries |
| `winner_role_id` | Role ID to ping on chicken dinner (0 = disabled) |
| `posted_matches_max_history` | How many match IDs to keep for deduplication |
| `birthday_channel_id` | Channel for birthday announcements |
| `pubg_channel_id` | Channel for the birthday chicken dinner crossover post |
| `birthday_announce_hour_utc` | Hour (UTC) to post birthday announcements daily |
| `birthday_role_name` | Must match the role name exactly in your Discord server |

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
python scrape_longest_kills.py
```

This populates `longest_kills_alltime.json` which powers the third embed in the weekly summary.

---

## Running as systemd services

### PUBG Bot

```bash
sudo nano /etc/systemd/system/pubgbot.service
```

```ini
[Unit]
Description=PUBG Discord Bot
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

### Birthday Bot

```bash
sudo nano /etc/systemd/system/birthdaybot.service
```

```ini
[Unit]
Description=Discord Birthday Bot
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/pubg-bot
ExecStart=/path/to/pubg-bot/venv/bin/python birthday_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Longest kills scraper (weekly, Wednesdays 15:00)

```bash
sudo nano /etc/systemd/system/pubg-scraper.service
```

```ini
[Unit]
Description=PUBG Longest Kills Scraper

[Service]
Type=oneshot
User=YOUR_USER
WorkingDirectory=/path/to/pubg-bot
ExecStart=/path/to/pubg-bot/venv/bin/python scrape_longest_kills.py
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
| `!weeklynow` | Admin | Manually trigger the weekly summary |
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
| `!birthdaytest` | Admin | Preview the birthday embed immediately |

---

## Weekly Summary

Every **Wednesday at 18:00 UTC** the bot automatically posts three embeds:

1. **Best Player of the Week** — top performer across kills, damage, wins, survival
2. **Leaderboard** — top 5 players ranked by composite score
3. **All-Time Longest Kills** — requires running `scrape_longest_kills.py` first

Trigger manually anytime with `!weeklynow`. Casual, Arcade, and Airoyale matches are excluded from all stats.

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
