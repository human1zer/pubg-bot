"""
shame.py — Wall of Shame: weekly award board + weekly digest lines of
shame-worthy plays, both posted together in the same weekly run.

Same data source as weekly_stats.py (the `matches` table via database.py),
same CASUAL/ARCADE/AIROYALE exclusion. Tone is dry and deadpan — state the
fact, no exclamation marks, no hype.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import discord

import database as db

logger = logging.getLogger(__name__)

SHAME_COLOR = discord.Color.dark_red()

AWARD_ORDER = [
    "Suicide King",
    "Teamkiller",
    "Roadkiller",
    "Blue Zone Food",
    "Quitter",
    "Worst KD",
]

WORST_KD_MIN_MATCHES = 10

DEFAULT_SHAME_TOP_N = 5


# ─────────────────────────────────────────────────────────────────────────────
# Weekly board
# ─────────────────────────────────────────────────────────────────────────────

async def calculate_weekly_shame(
    days: int = 7, top_n: int = DEFAULT_SHAME_TOP_N
) -> Optional[Tuple[Dict[str, List[Tuple[str, float]]], int]]:
    """
    Returns ({award_name: [(name, value), ...]}, total_matches). Each award
    lists up to `top_n` players, worst first, players with a value of 0
    excluded. An award is omitted entirely if nobody qualifies. Returns
    None if there's no match data at all, or if no award has a qualifying
    player.
    """
    players = await db.get_shame_stats(days)
    if not players:
        return None

    total_matches = sum(p["matches"] for p in players.values())

    awards: Dict[str, List[Tuple[str, float]]] = {}

    def ranked(metric: str) -> List[Tuple[str, int]]:
        scored = [(p["display_name"], p[metric]) for p in players.values() if p[metric] > 0]
        scored.sort(key=lambda t: (-t[1], t[0].lower()))
        return scored[:top_n]

    for award_name, metric in (
        ("Suicide King", "suicides"),
        ("Teamkiller", "team_kills"),
        ("Roadkiller", "road_kills"),
        ("Blue Zone Food", "byzone_deaths"),
        ("Quitter", "logouts"),
    ):
        result = ranked(metric)
        if result:
            awards[award_name] = result

    eligible = [p for p in players.values() if p["matches"] >= WORST_KD_MIN_MATCHES and p["deaths"] > 0]
    scored_kd = [(p["display_name"], round(p["kills"] / p["deaths"], 2)) for p in eligible]
    scored_kd = [(name, kd) for name, kd in scored_kd if kd > 0]
    scored_kd.sort(key=lambda t: (t[1], t[0].lower()))
    if scored_kd:
        awards["Worst KD"] = scored_kd[:top_n]

    if not awards:
        return None
    return awards, total_matches


def create_weekly_shame_embed(
    awards: Dict[str, List[Tuple[str, float]]], total_matches: int, days: int = 7
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Wall of Shame — Last {days} Days",
        color=SHAME_COLOR,
    )
    for award_name in AWARD_ORDER:
        if award_name not in awards:
            continue
        lines = []
        for i, (name, value) in enumerate(awards[award_name], start=1):
            display_value = f"{value:.2f}" if award_name == "Worst KD" else str(value)
            lines.append(f"{i}. {name} — {display_value}")
        embed.add_field(name=award_name, value="\n".join(lines), inline=False)
    embed.set_footer(text=f"Based on {total_matches} matches • {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    return embed


# ─────────────────────────────────────────────────────────────────────────────
# Weekly digest lines
# ─────────────────────────────────────────────────────────────────────────────

# Maps a digest category to the matching field in db.get_shame_stats()'s
# per-player dict, so the batched weekly-count query can be looked up by key.
CATEGORY_TO_WEEKLY_FIELD = {
    "suicide":  "suicides",
    "byzone":   "byzone_deaths",
    "quitter":  "logouts",
    "teamkill": "team_kills",
    "roadkill": "road_kills",
}

_ORDINAL_WORDS = {
    2: "Second", 3: "Third", 4: "Fourth", 5: "Fifth", 6: "Sixth",
    7: "Seventh", 8: "Eighth", 9: "Ninth", 10: "Tenth", 11: "Eleventh", 12: "Twelfth",
}


def _ordinal(n: int) -> str:
    if n in _ORDINAL_WORDS:
        return _ORDINAL_WORDS[n]
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# Phrasing pool for the running-count sentence. Each entry says which count
# it needs ("week" from the batched query, "today" from the same-day rows
# already in hand) — picking among templates that qualify (>= 2) is what
# gives the digest phrasing variety instead of repeating the same line.
_SECOND_SENTENCE_TEMPLATES = [
    ("week",  lambda n: f"That's {n} this week."),
    ("week",  lambda n: f"{_ordinal(n)} time this week."),
    ("today", lambda n: f"{_ordinal(n)} one today."),
    ("today", lambda n: f"That's {n} today."),
    ("week",  lambda n: f"{n} this week and counting."),
]


def _running_count_sentence(start_index: int, weekly_n: int, daily_n: int) -> Optional[str]:
    counts = {"week": weekly_n, "today": daily_n}
    for offset in range(len(_SECOND_SENTENCE_TEMPLATES)):
        period, template = _SECOND_SENTENCE_TEMPLATES[(start_index + offset) % len(_SECOND_SENTENCE_TEMPLATES)]
        n = counts[period]
        if n >= 2:
            return template(n)
    return None


def _facts_for_row(row: dict) -> List[Tuple[str, str, str]]:
    """Returns [(event_id, category, fact_line), ...] for one matches row — usually one, occasionally more."""
    facts: List[Tuple[str, str, str]] = []
    name   = row["player_name"]
    row_id = row["id"]

    death_type = row["death_type"]
    if death_type == "suicide":
        facts.append((f"{row_id}:suicide", "suicide", f"{name} killed themselves."))
    elif death_type == "byzone":
        facts.append((f"{row_id}:byzone", "byzone", f"{name} died to the blue zone."))
    elif death_type == "logout":
        facts.append((f"{row_id}:quitter", "quitter", f"{name} quit mid-match."))

    tk = row["team_kills"] or 0
    if tk > 0:
        count = "a" if tk == 1 else tk
        word  = "teammate" if tk == 1 else "teammates"
        facts.append((f"{row_id}:teamkill", "teamkill", f"{name} killed {count} {word}."))

    rk = row["road_kills"] or 0
    if rk > 0:
        count = "a" if rk == 1 else rk
        word  = "roadkill" if rk == 1 else "roadkills"
        facts.append((f"{row_id}:roadkill", "roadkill", f"{name} got {count} {word}."))

    return facts


def _compute_daily_counts(rows: List[dict]) -> Dict[Tuple[str, str], int]:
    """
    Per-(player, category) counts for today, built from the candidate rows
    already fetched for this digest run — no extra query.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    counts: Dict[Tuple[str, str], int] = {}

    def bump(key: Tuple[str, str], amount: int = 1) -> None:
        counts[key] = counts.get(key, 0) + amount

    for row in rows:
        if not row["played_at"].startswith(today):
            continue
        key = row["player_name"].lower()
        death_type = row["death_type"]
        if death_type == "suicide":
            bump((key, "suicide"))
        elif death_type == "byzone":
            bump((key, "byzone"))
        elif death_type == "logout":
            bump((key, "quitter"))
        tk = row["team_kills"] or 0
        if tk > 0:
            bump((key, "teamkill"), tk)
        rk = row["road_kills"] or 0
        if rk > 0:
            bump((key, "roadkill"), rk)

    return counts


async def get_weekly_shame_events(days: int = 7, max_lines: int = 25) -> Optional[Tuple[List[str], int]]:
    """
    Returns (lines, remaining_count) for every shame-worthy event in the
    last `days` — the same fixed window as the Wall of Shame board, posted
    once a week — most recent first, or None if there were none. `lines`
    is capped at `max_lines`; `remaining_count` is how many more were left
    out.

    Runs once a week over a fixed window, so there's no dedup against past
    runs — every qualifying event in range is included every time.
    """
    rows = await db.get_shame_candidate_rows(days)
    if not rows:
        return None

    events: List[Tuple[str, str, str]] = []  # category, name, fact_line
    for row in rows:  # already ordered played_at DESC
        for _event_id, category, fact_line in _facts_for_row(row):
            events.append((category, row["player_name"], fact_line))

    if not events:
        return None

    # One grouped query for this week's per-player/category counts — not one
    # per event — used to add the "that's N this week" style second sentence.
    weekly_players = await db.get_shame_stats(7)
    daily_counts   = _compute_daily_counts(rows)

    lines = []
    for i, (category, name, fact_line) in enumerate(events[:max_lines]):
        weekly_n = weekly_players.get(name.lower(), {}).get(CATEGORY_TO_WEEKLY_FIELD[category], 0)
        daily_n  = daily_counts.get((name.lower(), category), 0)
        sentence = _running_count_sentence(i, weekly_n, daily_n)
        lines.append(f"{fact_line} {sentence}" if sentence else fact_line)

    remaining = max(0, len(events) - max_lines)
    return lines, remaining


def format_digest_message(lines: List[str], remaining: int) -> str:
    text = "\n".join(lines)
    if remaining:
        text += f"\n+{remaining} more"
    return text
