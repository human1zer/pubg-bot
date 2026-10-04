#!/usr/bin/env python3
"""Match card prototype - step 3: full card (header + map + titles + stats).

Usage: venv/bin/python scripts/match_card.py <match_id> [player]
Output: scripts/.cache/<match_id>.png
"""
import gzip, json, math, os, sqlite3, sys, urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("PUBG_API_KEY")
ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".cache"
MAPS = CACHE / "maps"
MAPS.mkdir(parents=True, exist_ok=True)
DB = ROOT.parent / "pubg_bot.db"
TZ = ZoneInfo("Europe/Oslo")

# API mapName -> (asset name, map size in cm)
MAP_INFO = {
    "Baltic_Main": ("Erangel", 816000), "Erangel_Main": ("Erangel", 816000),
    "Desert_Main": ("Miramar", 816000), "Tiger_Main": ("Taego", 816000),
    "Kiki_Main": ("Deston", 816000), "Neon_Main": ("Rondo", 816000),
    "DihorOtok_Main": ("Vikendi", 816000), "Savage_Main": ("Sanhok", 408000),
    "Chimera_Main": ("Paramo", 306000), "Summerland_Main": ("Karakin", 204000),
    "Heaven_Main": ("Haven", 102000),
}
ASSET_URL = "https://media.githubusercontent.com/media/pubg/api-assets/master/Assets/Maps/{}_Main_High_Res.png"

COLORS = [(255, 214, 10), (0, 229, 255), (255, 120, 40), (235, 60, 255)]
W = 1280
BG = (16, 18, 22)
PANEL = (26, 29, 35)
MUTED = (140, 146, 158)
WHITE = (240, 242, 245)
GREEN = (61, 220, 132)
GOLD = (255, 200, 60)
RED = (255, 70, 70)
FB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FI = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"
_fonts = {}


def font(size, bold=True):
    key = (size, bold)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(FB if bold else FR, size)
    return _fonts[key]


# ---------------------------------------------------------------- data

def get(url, auth=True):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.api+json", "Accept-Encoding": "gzip"})
    if auth:
        req.add_header("Authorization", f"Bearer {API_KEY}")
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return json.loads(data)


def cached(name, fn):
    p = CACHE / name
    if p.exists():
        return json.loads(p.read_text())
    obj = fn()
    p.write_text(json.dumps(obj))
    return obj


def map_image(asset):
    p = MAPS / f"{asset}.png"
    if not p.exists():
        print(f"Downloading {asset} map (~95 MB, once)...")
        urllib.request.urlretrieve(ASSET_URL.format(asset), p)
    Image.MAX_IMAGE_PIXELS = None
    return Image.open(p).convert("RGB")


def tracked_names(match_id):
    if not DB.exists():
        return set()
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT player_name FROM matches WHERE match_id=?", (match_id,)).fetchall()
    con.close()
    return {r[0].lower() for r in rows}


def load(match_id, player):
    match = cached(f"{match_id}.match.json", lambda: get(
        f"https://api.pubg.com/shards/steam/matches/{match_id}"))
    attrs, inc = match["data"]["attributes"], match["included"]
    parts = {i["id"]: i["attributes"]["stats"] for i in inc if i["type"] == "participant"}
    rosters = [i for i in inc if i["type"] == "roster"]
    wanted = {player.lower()} if player else tracked_names(match_id)
    for r in rosters:
        team = [parts[p["id"]] for p in r["relationships"]["participants"]["data"]]
        if wanted & {m["name"].lower() for m in team}:
            tel_url = next(i["attributes"]["URL"] for i in inc if i["type"] == "asset")
            tel = cached(f"{match_id}.telemetry.json", lambda: get(tel_url, auth=False))
            return attrs, r["attributes"], len(rosters), team, tel
    sys.exit("No tracked player found in this match")


def ts(d):
    return datetime.fromisoformat(d.replace("Z", "+00:00")).timestamp()


def extract(team, tel):
    names = [m["name"] for m in team]
    nameset = set(names)
    landed = {}
    for e in tel:
        if e["_T"] == "LogParachuteLanding":
            n = e["character"]["name"]
            if n in nameset and n not in landed:
                landed[n] = e["_D"]

    paths = {n: [] for n in names}
    for e in tel:
        if e["_T"] != "LogPlayerPosition":
            continue
        c = e["character"]
        n = c["name"]
        if n in nameset and n in landed and e["_D"] >= landed[n]:
            loc = c["location"]
            veh = e.get("vehicle") or {}
            in_veh = bool(veh.get("vehicleType")) and veh.get("vehicleType") not in ("", "Parachute")
            paths[n].append((loc["x"], loc["y"], in_veh, ts(e["_D"])))

    kills, deaths = [], []
    for e in tel:
        if e["_T"] != "LogPlayerKillV2":
            continue
        victim = e.get("victim") or {}
        killer = e.get("killer") or e.get("finisher") or {}
        loc = victim.get("location")
        if not loc:
            continue
        if killer.get("name") in nameset and victim.get("name") not in nameset:
            kills.append((killer["name"], loc["x"], loc["y"], ts(e["_D"])))
        if victim.get("name") in nameset:
            deaths.append((victim["name"], loc["x"], loc["y"], ts(e["_D"])))

    # distinct white circles up to the moment the team's last player was out
    zones = []
    end_time = max((m["timeSurvived"] for m in team), default=0)
    for e in tel:
        if e["_T"] != "LogGameStatePeriodic":
            continue
        gs = e["gameState"]
        if gs.get("elapsedTime", 0) > end_time:
            break
        pos, r = gs.get("poisonGasWarningPosition"), gs.get("poisonGasWarningRadius")
        if not pos or not r:
            continue
        z = (pos["x"], pos["y"], r)
        if not zones or abs(zones[-1][2] - r) > 1:
            zones.append(z)
    return names, paths, kills, deaths, zones[-4:]


# ---------------------------------------------------------------- titles

def mmss(sec):
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def pl(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def impact(m):
    return m["damageDealt"] + m["kills"] * 100 + m["DBNOs"] * 50 + m["assists"] * 30 + m["revives"] * 40


# (title, value fn, label fn) - assigned in this order, one per player
TITLES = [
    ("MVP", impact, lambda m: f"{m['damageDealt']:.0f} dmg · {pl(m['kills'], 'kill')}"),
    ("Fragger", lambda m: m["kills"], lambda m: pl(m['kills'], 'kill')),
    ("Sniper", lambda m: m["longestKill"] if m["longestKill"] >= 100 else 0, lambda m: f"{m['longestKill']:.0f}m kill"),
    ("Medic", lambda m: m["revives"], lambda m: pl(m['revives'], 'revive')),
    ("Headhunter", lambda m: m["headshotKills"], lambda m: pl(m['headshotKills'], 'headshot')),
    ("Support", lambda m: m["assists"], lambda m: pl(m['assists'], 'assist')),
    ("Damage Dealer", lambda m: m["damageDealt"], lambda m: f"{m['damageDealt']:.0f} dmg"),
    ("Wheelman", lambda m: m["rideDistance"] if m["rideDistance"] >= 1000 else 0, lambda m: f"{m['rideDistance'] / 1000:.1f} km driven"),
    ("Pharmacist", lambda m: m["heals"] + m["boosts"], lambda m: f"{m['heals'] + m['boosts']} heals & boosts"),
    ("Survivor", lambda m: m["timeSurvived"], lambda m: f"alive {mmss(m['timeSurvived'])}"),
]


def assign_titles(team):
    titles = {}
    left = list(team)
    if len(team) >= 2:
        worst = min(team, key=lambda m: (impact(m), m["timeSurvived"]))
    if len(team) >= 2 and worst["damageDealt"] < 100 and worst["kills"] == 0:
        titles[worst["name"]] = ("CHICKEN", f"{worst['damageDealt']:.0f} dmg · out at {mmss(worst['timeSurvived'])}", RED)
        left.remove(worst)
    for title, val, label in TITLES:
        if not left:
            break
        best = max(left, key=val)
        if val(best) > 0:
            titles[best["name"]] = (title, label(best), GOLD)
            left.remove(best)
    for m in left:
        titles[m["name"]] = ("Tourist", f"walked {m['walkDistance'] / 1000:.1f} km", MUTED)
    return titles


# ---------------------------------------------------------------- roast

def load_cfg():
    try:
        return json.loads((ROOT.parent / "config.json").read_text())
    except Exception:
        return {}


def roast(asset, rank, n_teams, team, titles):
    """One sarcastic line from the local Ollama, same persona as !ask."""
    cfg = load_cfg()
    url = cfg.get("ask_ollama_url") or "http://localhost:11434/api/chat"
    model = cfg.get("ask_model") or "llama3.2:3b"
    system = cfg.get("ask_system_prompt") or "You're a sarcastic longtime member of this PUBG clan's Discord."
    for lp in (ROOT.parent / "lore.md", ROOT.parent / "data" / "lore.md"):
        if lp.exists():
            system += "\n\nWhat you know about the group:\n" + lp.read_text()[:4000]
            break

    result = "WON the match (chicken dinner)" if rank == 1 else f"placed #{rank} of {n_teams} teams"
    facts = [f"Result: {result} on {asset}."]
    for m in team:
        t = titles[m["name"]][0]
        facts.append(f"- {m['name']}: {m['kills']} kills, {m['damageDealt']:.0f} damage, {m['DBNOs']} knocks, "
                     f"{m['revives']} revives, survived {mmss(m['timeSurvived'])}, award: {t}")
    prompt = ("Your squad just finished a PUBG match:\n" + "\n".join(facts) +
              "\n\nReact in the group chat with ONE short line (max 25 words) in English. "
              "Dry and sarcastic, roast whoever deserves it, use their names. Only use the facts above, never invent events or numbers. Output only the line.")
    payload = {
        "model": model, "stream": False, "keep_alive": cfg.get("ask_keep_alive", "5m"),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "options": {"num_ctx": 4096, "num_predict": 80, "temperature": 0.9},
    }
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            text = json.loads(r.read())["message"]["content"].strip()
    except Exception as e:
        print(f"Roast skipped: {e!r}")
        return None
    text = text.splitlines()[0].strip() if text else ""
    first = text.split(" ", 1)[0]
    if first.endswith(":"):          # model sometimes answers as "Name: ..."
        text = text[len(first):].strip()
    text = text.strip('"“”').strip()
    return text[:220] or None


def wrap(text, f, width, max_lines=3):
    lines, cur = [], ""
    for w in text.split():
        t = f"{cur} {w}".strip()
        if f.getlength(t) <= width:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines[:max_lines]


# ---------------------------------------------------------------- map

def crop_box(paths, kills, deaths, zone, size):
    xs, ys = [], []
    for p in paths.values():
        xs += [pt[0] for pt in p]
        ys += [pt[1] for pt in p]
    for _, x, y, _t in kills + deaths:
        xs.append(x); ys.append(y)
    if zone:
        x, y, r = zone[-1]
        xs += [x - r, x + r]; ys += [y - r, y + r]
    if not xs:
        return 0, 0, size
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    side = max(max(xs) - min(xs), max(ys) - min(ys)) * 1.2
    side = min(max(side, size * 0.2), size)
    x0 = min(max(cx - side / 2, 0), size - side)
    y0 = min(max(cy - side / 2, 0), size - side)
    return x0, y0, side


def dashed(d, xy, fill, width, dash, gap):
    carry = 0.0
    on = True
    for (x1, y1), (x2, y2) in zip(xy, xy[1:]):
        seg = math.hypot(x2 - x1, y2 - y1)
        pos = 0.0
        while pos < seg:
            step = min((dash if on else gap) - carry, seg - pos)
            if on:
                a = pos / seg
                b = (pos + step) / seg
                d.line([(x1 + (x2 - x1) * a, y1 + (y2 - y1) * a),
                        (x1 + (x2 - x1) * b, y1 + (y2 - y1) * b)], fill=fill, width=width)
            pos += step
            carry += step
            if carry >= (dash if on else gap) - 1e-6:
                carry = 0.0
                on = not on


def draw_layer(base, x0, y0, side, names, color, paths, kills, deaths, zone, legend=True):
    """Draw zones, paths, kills and deaths on a square map crop."""
    out = base.width
    s = out / side
    P = lambda x, y: ((x - x0) * s, (y - y0) * s)
    lw = max(3, out // 300)
    img = Image.blend(base, Image.new("RGB", base.size, (0, 0, 0)), 0.25).convert("RGBA")

    if zone:
        zl = Image.new("RGBA", img.size, (0, 0, 0, 0))
        zd = ImageDraw.Draw(zl)
        for i, (zx, zy, zr) in enumerate(zone):
            last = i == len(zone) - 1
            zx, zy = P(zx, zy)
            zr = max(zr * s, 14)
            zd.ellipse([zx - zr, zy - zr, zx + zr, zy + zr],
                       fill=(255, 255, 255, 60) if last else None,
                       outline=(255, 255, 255, 255 if last else 120), width=lw if last else 2)
        img = Image.alpha_composite(img, zl)

    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    n_players = len(names)
    for i, n in enumerate(names):
        pts = paths[n]
        if len(pts) < 2:
            continue
        off = (i - (n_players - 1) / 2) * lw * 1.3   # spread overlapping squad paths
        # split into runs of on-foot / in-vehicle
        runs, cur, mode = [], [], None
        for x, y, veh, _t in pts:
            px, py = P(x, y)
            pt = (px + off, py + off)
            if mode is None or veh == mode:
                cur.append(pt)
            else:
                runs.append((mode, cur))
                cur = [cur[-1], pt]
            mode = veh
        runs.append((mode, cur))
        for veh, xy in runs:
            if len(xy) < 2:
                continue
            if veh:
                dashed(d, xy, color[n] + (255,), max(2, lw - 1), lw * 4, lw * 3)
            else:
                d.line(xy, fill=color[n] + (80,), width=lw * 3, joint="curve")
                d.line(xy, fill=color[n] + (255,), width=lw, joint="curve")
        lx, ly = P(pts[0][0], pts[0][1])
        lx, ly = lx + off, ly + off
        r = lw * 2.5
        d.ellipse([lx - r, ly - r, lx + r, ly + r], fill=color[n] + (255,), outline=(0, 0, 0, 255), width=2)

    m = lw * 4
    for n, x, y, _t in kills:
        px, py = P(x, y)
        for a, b in (((px - m, py - m), (px + m, py + m)), ((px - m, py + m), (px + m, py - m))):
            d.line([a, b], fill=(0, 0, 0, 255), width=lw + 4)
            d.line([a, b], fill=color[n] + (255,), width=lw)

    for n, x, y, _t in deaths:
        px, py = P(x, y)
        r = m * 1.2
        d.ellipse([px - r, py - r, px + r, py + r], fill=(200, 20, 20, 255), outline=color[n] + (255,), width=lw + 2)
        d.line([(px - r * .5, py - r * .5), (px + r * .5, py + r * .5)], fill=(255, 255, 255, 255), width=lw)
        d.line([(px - r * .5, py + r * .5), (px + r * .5, py - r * .5)], fill=(255, 255, 255, 255), width=lw)

    return Image.alpha_composite(img, ov)


def final_fight_box(paths, kills, deaths, window=120, min_side=30000):
    """Square around everything the team did in its last `window` seconds."""
    times = [p[3] for pts in paths.values() for p in pts] + [k[3] for k in kills + deaths]
    if not times:
        return None
    end = max(times)
    xs, ys = [], []
    for pts in paths.values():
        for x, y, _v, t in pts:
            if t >= end - window:
                xs.append(x); ys.append(y)
    for _n, x, y, t in kills + deaths:
        if t >= end - window:
            xs.append(x); ys.append(y)
    if not xs:
        return None
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    side = max(max(xs) - min(xs), max(ys) - min(ys), min_side) * 1.4
    return cx - side / 2, cy - side / 2, side


def draw_map(asset, size, names, color, paths, kills, deaths, zone):
    x0, y0, side = crop_box(paths, kills, deaths, zone, size)
    full = map_image(asset)
    k = full.width / size

    def crop(cx0, cy0, cside, out):
        im = full.crop((int(cx0 * k), int(cy0 * k), int((cx0 + cside) * k), int((cy0 + cside) * k)))
        return im.resize((out, out), Image.LANCZOS)

    img = draw_layer(crop(x0, y0, side, W), x0, y0, side, names, color, paths, kills, deaths, zone)

    # final-fight inset when the action at the end is small relative to the main view
    fb = final_fight_box(paths, kills, deaths)
    if fb and fb[2] < side * 0.35:
        IN = 420
        fx0, fy0, fside = fb
        inset = draw_layer(crop(fx0, fy0, fside, IN), fx0, fy0, fside, names, color,
                           paths, kills, deaths, zone)
        # outline of the inset area on the main map
        d = ImageDraw.Draw(img)
        s = W / side
        bx, by = (fx0 - x0) * s, (fy0 - y0) * s
        d.rectangle([bx, by, bx + fside * s, by + fside * s], outline=(255, 255, 255, 220), width=2)
        # place inset in the free corner covering the fewest path points / the box
        pts = [((x - x0) * s, (y - y0) * s) for pl in paths.values() for x, y, _v, _t in pl]
        box = (bx, by, bx + fside * s, by + fside * s)

        def cost(cx, cy):
            inside = sum(cx <= px <= cx + IN and cy <= py <= cy + IN for px, py in pts)
            overlap = not (box[2] < cx or box[0] > cx + IN or box[3] < cy or box[1] > cy + IN)
            return inside + (10 ** 6 if overlap else 0)

        corners = [(W - IN - 20, 20), (W - IN - 20, W - IN - 76), (20, W - IN - 76)]
        ix, iy = min(corners, key=lambda c: cost(*c))
        d.rectangle([ix - 4, iy - 4, ix + IN + 4, iy + IN + 4], fill=(255, 255, 255, 255))
        img.paste(inset, (ix, iy))
        tag = "FINAL FIGHT"
        tw = d.textlength(tag, font=font(22))
        d.rectangle([ix, iy, ix + tw + 20, iy + 34], fill=(0, 0, 0, 200))
        d.text((ix + 10, iy + 5), tag, font=font(22), fill=WHITE)
    del full

    # legend
    d = ImageDraw.Draw(img)
    f = font(28)
    y = 22
    for n in names:
        tw = d.textlength(n, font=f)
        d.rounded_rectangle([20, y - 6, 20 + 46 + tw + 16, y + 38], 10, fill=(0, 0, 0, 175))
        d.ellipse([32, y + 5, 54, y + 27], fill=color[n])
        d.text((66, y), n, font=f, fill=WHITE)
        y += 52

    # map key, bottom-left
    key = [("✕", "kill"), ("●", "death"), ("╌", "vehicle"), ("○", "zone")]
    x, y = 20, W - 56
    kw = 16 + sum(32 + d.textlength(t, font=font(22, False)) + 26 for _, t in key)
    d.rounded_rectangle([x, y - 8, x + kw - 10, y + 38], 10, fill=(0, 0, 0, 175))
    x += 16
    for sym, txt in key:
        d.text((x, y), sym, font=font(26), fill=RED if txt == "death" else WHITE)
        x += 32
        d.text((x, y + 2), txt, font=font(22, False), fill=WHITE)
        x += d.textlength(txt, font=font(22, False)) + 26
    return img.convert("RGB")


# ---------------------------------------------------------------- card

def draw_center(d, cx, y, text, f, fill):
    d.text((cx - d.textlength(text, font=f) / 2, y), text, font=f, fill=fill)


def draw_right(d, rx, y, text, f, fill):
    d.text((rx - d.textlength(text, font=f), y), text, font=f, fill=fill)


def render(match_id, player=None):
    attrs, roster, n_teams, team, tel = load(match_id, player)
    asset, size = MAP_INFO.get(attrs["mapName"], (None, None))
    if not asset:
        sys.exit(f"Unknown map {attrs['mapName']}")
    names, paths, kills, deaths, zone = extract(team, tel)
    team.sort(key=impact, reverse=True)
    names = [m["name"] for m in team]
    color = {n: COLORS[i % len(COLORS)] for i, n in enumerate(names)}
    titles = assign_titles(team)

    rank = roster["stats"]["rank"]
    won = rank == 1
    played = datetime.fromisoformat(attrs["createdAt"].replace("Z", "+00:00")).astimezone(TZ)

    HEADER, ROW, TABLE_HEAD, PAD = 210, 118, 64, 30
    quote = roast(asset, rank, n_teams, team, titles)
    f_q = ImageFont.truetype(FI if os.path.exists(FI) else FR, 30)
    qlines = wrap(f"“{quote}”", f_q, W - 110) if quote else []
    RB = 40 * len(qlines) + 40 if qlines else 0
    H = HEADER + RB + W + PAD + TABLE_HEAD + ROW * len(team) + PAD + 50
    card = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(card)

    # header
    accent = GREEN if won else WHITE
    big = "WINNER WINNER CHICKEN DINNER!" if won else f"#{rank}"
    f_big = font(64)
    while d.textlength(big, font=f_big) > W - 80:
        f_big = font(f_big.size - 2)
    d.text((40, 34), big, font=f_big, fill=accent)
    if not won:
        bx = 40 + d.textlength(big, font=f_big) + 14
        d.text((bx, 64), f"/ {n_teams}", font=font(34), fill=MUTED)
    sub = f"{asset}  ·  {attrs['gameMode'].replace('-fpp', ' FPP').title()}  ·  {played:%a %d %b %H:%M}  ·  {mmss(attrs['duration'])}"
    d.text((40, 124), sub, font=font(30, False), fill=MUTED)
    tk = sum(m["kills"] for m in team)
    td = sum(m["damageDealt"] for m in team)
    draw_right(d, W - 40, 124, f"{pl(tk, 'kill')} · {td:.0f} dmg", font(30), WHITE)
    d.rectangle([0, HEADER - 6, W, HEADER - 2], fill=accent)

    # roast line from Ollama
    if qlines:
        y = HEADER + 18
        d.rectangle([40, y + 2, 46, y + 40 * len(qlines) - 4], fill=accent)
        for ln in qlines:
            d.text((64, y), ln, font=f_q, fill=WHITE)
            y += 40
        HEADER += RB

    # map
    card.paste(draw_map(asset, size, names, color, paths, kills, deaths, zone), (0, HEADER))

    # table
    y = HEADER + W + PAD
    cols = [("K", 700), ("DMG", 830), ("KN", 920), ("AST", 1010), ("REV", 1090), ("SURV", W - 40)]
    for label, rx in cols:
        draw_right(d, rx, y + 18, label, font(24), MUTED)
    y += TABLE_HEAD
    for m in team:
        n = m["name"]
        t, tl, tc = titles[n]
        d.rounded_rectangle([30, y, W - 30, y + ROW - 14], 14, fill=PANEL)
        d.rounded_rectangle([30, y, 40, y + ROW - 14], 4, fill=color[n])
        d.text((64, y + 12), n, font=font(36), fill=WHITE)
        tag = t.upper()
        tw = d.textlength(tag, font=font(22))
        d.rounded_rectangle([64, y + 60, 64 + tw + 22, y + 92], 8, fill=tc)
        d.text((75, y + 63), tag, font=font(22), fill=BG)
        d.text((64 + tw + 36, y + 63), tl, font=font(22, False), fill=MUTED)
        vals = [m["kills"], f"{m['damageDealt']:.0f}", m["DBNOs"], m["assists"], m["revives"], mmss(m["timeSurvived"])]
        for (label, rx), v in zip(cols, vals):
            draw_right(d, rx, y + 32, str(v), font(36), WHITE)
        y += ROW

    draw_right(d, W - 40, H - 46, "PUSH · GayAPP", font(20, False), MUTED)

    out = CACHE / f"{match_id}.png"
    card.save(out, optimize=True)
    print(f"Saved {out}")
    print(f"Rank #{rank}/{n_teams} | kills marked: {len(kills)} | deaths: {len(deaths)}")
    print(f"Roast: {quote}")
    for n in names:
        print(f"  {n:<18} {titles[n][0]:<14} {titles[n][1]}")


if __name__ == "__main__":
    render(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
