"""
Group lore for !ask — short notes about the group (nicknames, topics, inside
jokes, running gags) that a local Ollama model distils from chat every night.

Only the distilled notes are kept (in lore.md); raw messages are never written
to disk. Each note carries the date it was last seen, so notes that stop
coming up age out and the file stays under a token budget.
"""
import datetime
import difflib
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

LORE_PATH = Path(__file__).with_name("lore.md")

CATEGORIES = {
    "nicknames": "Nicknames",
    "topics":    "Recurring topics",
    "jokes":     "Inside jokes",
    "gags":      "Running gags",
}
HEADER_TO_CATEGORY = {v: k for k, v in CATEGORIES.items()}

NUM_CTX          = 4096   # context window used for every Ollama call
CHUNK_TOKENS     = 2400   # chat tokens per extraction call (rest = prompt + output)
EXTRACT_PREDICT  = 512
MERGE_BATCH      = 25     # new notes per merge call
MERGE_PREDICT    = 900
MAX_LINE_CHARS   = 500    # a single chat message is truncated to this
SIMILAR_RATIO    = 0.85   # fuzzy match threshold for "same note"

_LINE_RE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2})\] (.+)$")
# Small models like to fill empty categories with non-notes
_JUNK_RE = re.compile(r"\b(not (mentioned|specified|clear|known)|no (nickname|notable|jokes?|gags?)|unknown|n/a|none)\b", re.I)

EXTRACT_PROMPT = (
    "You read a chunk of a Discord group chat between friends in a PUBG clan and write "
    "short notes for the group's shared memory. Only note things worth remembering "
    "long-term:\n"
    "- nicknames: who goes by which nickname, or what people call each other\n"
    "- topics: things the group keeps talking about\n"
    "- jokes: inside jokes and memes the group uses\n"
    "- gags: running gags, recurring teasing, rivalries\n"
    "A speaker shown as 'Nick (username)' has display name Nick and username username.\n"
    "Rules: each note is one complete sentence, max 15 words, in English, and names the "
    "people involved. Do not quote whole messages. Never note private or sensitive details "
    "(addresses, phone numbers, health, money, relationships, passwords, real-life "
    "problems). One-off small talk is not lore. Only write what the chat actually shows — "
    "never guess. Nicknames: only when someone is called something other than their "
    "display name, written as 'username goes by Nick'. Use empty lists if nothing is notable."
)

MERGE_PROMPT = (
    "You maintain a list of notes about a friend group. You get the EXISTING notes "
    "(numbered) and NEW candidate notes from today's chat (lettered). For every "
    "candidate output one item:\n"
    "- candidate: the candidate's letter\n"
    "- same_as: the number of the existing note about the same person and thing, or 0 "
    "if it is new\n"
    "- keep: false if the candidate is trivial, vague, or a private/sensitive detail; "
    "otherwise true"
)

_STRING_LIST = {"type": "array", "items": {"type": "string"}}
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {k: _STRING_LIST for k in CATEGORIES},
    "required": list(CATEGORIES),
}
MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate": {"type": "string"},
                    "same_as":   {"type": "integer"},
                    "keep":      {"type": "boolean"},
                },
                "required": ["candidate", "same_as", "keep"],
            },
        },
    },
    "required": ["items"],
}


def estimate_tokens(text: str) -> int:
    # Conservative (~3 chars/token) — chat has emoji and non-English text.
    return len(text) // 3 + 1


# ─────────────────────────────────────────────────────────────────────────────
# lore.md read / write
# ─────────────────────────────────────────────────────────────────────────────

def load_notes(path: Path = None) -> list:
    """Parse lore.md into [{"category", "text", "date"}]. Missing file → []."""
    path = path or LORE_PATH
    if not path.exists():
        return []
    notes, category = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            category = HEADER_TO_CATEGORY.get(line[3:].strip())
            continue
        m = _LINE_RE.match(line.strip())
        if m and category:
            try:
                date = datetime.date.fromisoformat(m.group(1))
            except ValueError:
                continue
            notes.append({"category": category, "text": m.group(2).strip(), "date": date})
    return notes


def render_file(notes: list) -> str:
    out = ["# Lore", "", "_Auto-generated nightly from chat. Dates = last seen._"]
    for key, header in CATEGORIES.items():
        items = sorted((n for n in notes if n["category"] == key), key=lambda n: n["date"], reverse=True)
        if items:
            out += ["", f"## {header}"]
            out += [f"- [{n['date'].isoformat()}] {n['text']}" for n in items]
    return "\n".join(out) + "\n"


def render_prompt(notes: list) -> str:
    """Lore as it goes into the !ask prompt — no dates, no preamble."""
    out = []
    for key, header in CATEGORIES.items():
        items = [n["text"] for n in notes if n["category"] == key]
        if items:
            out.append(f"{header}:\n" + "\n".join(f"- {t}" for t in items))
    return "\n".join(out)


def save_notes(notes: list, path: Path = None) -> None:
    path = path or LORE_PATH
    tmp = path.with_suffix(".tmp")
    tmp.write_text(render_file(notes), encoding="utf-8")
    os.replace(tmp, path)   # atomic — !ask never reads a half-written file


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.lower()).strip()


def _find_similar(notes: list, category: str, text: str):
    target = _norm(text)
    best, best_ratio = None, SIMILAR_RATIO
    for n in notes:
        if n["category"] != category:
            continue
        ratio = difflib.SequenceMatcher(None, _norm(n["text"]), target).ratio()
        if ratio >= best_ratio:
            best, best_ratio = n, ratio
    return best


def chunk_lines(lines: list, budget: int = CHUNK_TOKENS) -> list:
    """Group chat lines into chunks of at most `budget` estimated tokens."""
    chunks, current, used = [], [], 0
    for line in lines:
        line = line[:MAX_LINE_CHARS]
        cost = estimate_tokens(line)
        if current and used + cost > budget:
            chunks.append("\n".join(current))
            current, used = [], 0
        current.append(line)
        used += cost
    if current:
        chunks.append("\n".join(current))
    return chunks


def prune(notes: list, today: datetime.date, stale_days: int, max_tokens: int) -> list:
    """Dedupe, drop notes not seen for `stale_days`, then drop oldest until under budget."""
    kept, seen = [], set()
    for n in sorted(notes, key=lambda n: n["date"], reverse=True):
        key = (n["category"], _norm(n["text"]))
        if not key[1] or key in seen:
            continue
        if (today - n["date"]).days > stale_days:
            continue
        seen.add(key)
        kept.append(n)
    while kept and estimate_tokens(render_prompt(kept)) > max_tokens:
        kept.pop()   # sorted newest first → drops the oldest
    return kept


def _letter(i: int) -> str:
    return chr(ord("A") + i) if i < 26 else f"Z{i}"


def apply_merge(notes: list, batch: list, verdicts: list, today: datetime.date) -> None:
    """
    Apply one merge batch in place. `batch` is [(category, text)] candidates,
    `verdicts` the model's items ({candidate letter, same_as 1-based, keep}).
    The model only classifies — note text and category always come from the candidate.
    Candidates the model skipped fall back to fuzzy matching.
    """
    by_letter = {}
    for v in verdicts:
        if isinstance(v, dict):
            by_letter.setdefault(str(v.get("candidate", "")).strip().upper(), v)
    existing = list(notes)   # same_as refers to the list as sent to the model
    for i, (category, text) in enumerate(batch):
        v = by_letter.get(_letter(i), {})
        if v.get("keep") is False:
            continue
        same_as = v.get("same_as")
        match = existing[same_as - 1] if isinstance(same_as, int) and 1 <= same_as <= len(existing) else None
        if match is not None and match["category"] != category:
            match = None   # never let a match move a note into another section
        match = match or _find_similar(notes, category, text)
        if match:
            if len(text) > len(match["text"]):   # newer and more specific wording wins
                match["text"] = text
            match["date"] = today
        else:
            notes.append({"category": category, "text": text, "date": today})


# ─────────────────────────────────────────────────────────────────────────────
# Nightly update
# ─────────────────────────────────────────────────────────────────────────────

async def extract_notes(chat, chunk: str) -> list:
    """One extraction call → [(category, text)]."""
    data = await chat(
        [{"role": "system", "content": EXTRACT_PROMPT},
         {"role": "user", "content": f"Chat:\n{chunk}"}],
        schema=EXTRACT_SCHEMA, num_predict=EXTRACT_PREDICT,
    )
    notes = []
    for key in CATEGORIES:
        for t in data.get(key, []) or []:
            text = " ".join(str(t).split())
            if len(text) >= 8 and not _JUNK_RE.search(text):
                notes.append((key, text))
    return notes


async def merge_batch(chat, notes: list, batch: list) -> list:
    existing = "\n".join(f"{i}. [{n['category']}] {n['text']}" for i, n in enumerate(notes, 1)) or "(none)"
    new = "\n".join(f"{_letter(i)}. [{cat}] {text}" for i, (cat, text) in enumerate(batch))
    data = await chat(
        [{"role": "system", "content": MERGE_PROMPT},
         {"role": "user", "content": f"EXISTING notes:\n{existing}\n\nNEW candidates:\n{new}"}],
        schema=MERGE_SCHEMA, num_predict=MERGE_PREDICT,
    )
    return data.get("items", []) or []


async def update_lore(chat, lines: list, today: datetime.date, stale_days: int, max_tokens: int,
                      path: Path = None) -> dict:
    """
    Run the nightly pipeline over formatted chat lines ("Name: message").
    `chat(messages, schema, num_predict)` performs one Ollama call and returns parsed JSON.
    Returns stats for logging. The lines are only held in memory.
    """
    notes = load_notes(path)
    before = len(notes)

    candidates, chunks, failed = [], chunk_lines(lines), 0
    for chunk in chunks:
        try:
            candidates += await extract_notes(chat, chunk)
        except Exception as e:
            failed += 1
            logger.warning(f"⚠️ Lore extraction failed for one chunk: {e!r}")
    if chunks and failed == len(chunks):
        raise RuntimeError("every lore extraction call failed — is Ollama running?")

    # Collapse near-identical candidates before spending model calls on them
    unique = []
    for cat, text in candidates:
        if not any(c == cat and difflib.SequenceMatcher(None, _norm(t), _norm(text)).ratio() >= SIMILAR_RATIO
                   for c, t in unique):
            unique.append((cat, text))

    for i in range(0, len(unique), MERGE_BATCH):
        batch = unique[i:i + MERGE_BATCH]
        try:
            verdicts = await merge_batch(chat, notes, batch)
        except Exception as e:
            # Fall back to plain fuzzy dedupe so a flaky merge call doesn't lose the day
            logger.warning(f"⚠️ Lore merge call failed, using fuzzy dedupe: {e!r}")
            verdicts = []
        apply_merge(notes, batch, verdicts, today)
        # Keep the EXISTING list inside the budget for the next batch's prompt
        notes = prune(notes, today, stale_days, max_tokens)

    notes = prune(notes, today, stale_days, max_tokens)
    save_notes(notes, path)
    return {
        "messages": len(lines), "chunks": len(chunks), "failed_chunks": failed,
        "candidates": len(unique),
        "before": before, "after": len(notes),
        "tokens": estimate_tokens(render_prompt(notes)),
    }
