#!/usr/bin/env python3
"""Render about.svg: GitHub avatar as ASCII art (left) + about.md as a table (right).

Usage:
    python scripts/render_about.py [--user NAME] [--about about.md] [--out about.svg]
                                   [--cols 44] [--mode color|mono] [--avatar local.png]

Env:
    ENV_BIRTHDAY   Birthday as DD.MM.YYYY -> fills {{UPTIME}} and {{AGE}}
    GITHUB_TOKEN   Needed for {{CONTRIBUTED}} (GraphQL); also avoids REST rate limits

about.md format (Markdown, readable on its own):
    # title                       -> header line (e.g. schmarante@github)
    ## Section                    -> section divider
    - Key: Value                  -> table row ("- **Key:** Value" works too)
    > Quote                       -> italic quote with accent bar, at that position
    | Key | Value |               -> table row (header/separator rows are skipped)
Placeholders: {{UPTIME}} {{AGE}} {{REPOS}} {{CONTRIBUTED}} {{STARS}} {{COMMITS}}
              {{LOC}} {{LOC_ADD}} {{LOC_DEL}} {{FOLLOWERS}} {{UPDATED}}
"""

from __future__ import annotations

import argparse
import calendar
import io
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from html import escape

from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps

# ---- layout -----------------------------------------------------------------
FONT_SIZE = 13                    # table text
CHAR_W = FONT_SIZE * 0.6          # monospace advance width; enforced via textLength
LINE_H = 16
ART_FONT_SIZE = FONT_SIZE         # same size as the table text
ART_CHAR_W = CHAR_W               # cell width, enforced via textLength
ART_LINE_H = 15                   # a bit tighter than the table so █ rows touch
PAD = 24
TITLEBAR = 22
GAP = 36                          # px between portrait and table
BG_KEY = (30, 95)                # flood fill: max RGB step between neighbours, max distance to border colour

# Ramp from dark to bright (dark background -> bright pixels get dense glyphs)
RAMP = " ░▒▓█"
ART_BRIGHTNESS = 1.35             # colour boost for the portrait (1.0 = original avatar)
ART_CIRCLE = True                 # round crop of the portrait
ART_REMOVE_BG = False             # True -> hide the avatar background (flood fill from the border)
ART_GAMMA = 0.75                  # <1 -> more dense blocks (brighter), >1 -> more sparse blocks

THEME = {
    "bg": "#0d1117",
    "border": "#30363d",
    "art": "#8b949e",
    "title": "#e6edf3",
    "rule": "#484f58",
    "section": "#7ee787",
    "key": "#ffa657",
    "dots": "#484f58",
    "value": "#a5d6ff",
    "quote": "#e6edf3",
    "qbar": "#d2a8ff",
    "add": "#3fb950",
    "del": "#f85149",
}

API = "https://api.github.com"


# ---- data -------------------------------------------------------------------
def http_get(url: str, token: str | None = None, accept: str | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "about-svg-renderer"})
    if accept:
        req.add_header("Accept", accept)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def fmt(n: int) -> str:
    return f"{n:,}"


def contributor_stats(repo: str, user: str, token: str | None) -> tuple[int, int, int]:
    """(commits, additions, deletions) by `user` on the default branch of `repo`."""
    url = f"{API}/repos/{repo}/stats/contributors"
    for attempt in range(8):
        req = urllib.request.Request(url, headers={"User-Agent": "about-svg-renderer"})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, body = resp.status, resp.read()
        if status == 200 and body:
            break
        if status == 204:  # empty repository
            return 0, 0, 0
        time.sleep(2 + attempt)  # 202: GitHub is still computing the stats
    else:
        raise RuntimeError(f"stats for {repo} not ready")
    for entry in json.loads(body):
        if (entry.get("author") or {}).get("login", "").lower() == user.lower():
            weeks = entry["weeks"]
            return entry["total"], sum(w["a"] for w in weeks), sum(w["d"] for w in weeks)
    return 0, 0, 0


def contributed_repos(user: str, token: str | None) -> list[str]:
    """Repos the user committed to / opened PRs or issues in (GraphQL, needs a token)."""
    if not token:
        raise RuntimeError("GITHUB_TOKEN not set")
    names, cursor = [], None
    while True:
        query = {
            "query": """query($login: String!, $after: String) {
              user(login: $login) {
                repositoriesContributedTo(first: 100, after: $after, includeUserRepositories: true,
                  contributionTypes: [COMMIT, PULL_REQUEST, ISSUE, REPOSITORY]) {
                  nodes { nameWithOwner }
                  pageInfo { hasNextPage endCursor }
                }
              }
            }""",
            "variables": {"login": user, "after": cursor},
        }
        req = urllib.request.Request(
            f"{API}/graphql", data=json.dumps(query).encode(),
            headers={"User-Agent": "about-svg-renderer", "Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if "errors" in data:
            raise RuntimeError(data["errors"][0].get("message"))
        conn = data["data"]["user"]["repositoriesContributedTo"]
        names += [n["nameWithOwner"] for n in conn["nodes"]]
        if not conn["pageInfo"]["hasNextPage"]:
            return names
        cursor = conn["pageInfo"]["endCursor"]


def github_stats(user: str, token: str | None) -> dict[str, str]:
    """Each stat degrades to "?" on its own, the render never fails because of the API."""
    out = {k: "?" for k in ("REPOS", "FOLLOWERS", "STARS", "CONTRIBUTED", "COMMITS", "LOC", "LOC_ADD", "LOC_DEL")}
    try:
        profile = json.loads(http_get(f"{API}/users/{user}", token))
        repos, page = [], 1
        while True:
            batch = json.loads(http_get(f"{API}/users/{user}/repos?per_page=100&page={page}", token))
            repos += batch
            if len(batch) < 100:
                break
            page += 1
        out["REPOS"] = fmt(profile["public_repos"])
        out["FOLLOWERS"] = fmt(profile["followers"])
        out["STARS"] = fmt(sum(r["stargazers_count"] for r in repos if not r["fork"]))
    except Exception as exc:
        print(f"warning: GitHub profile unavailable ({exc})", file=sys.stderr)
        return out

    own = [r["full_name"] for r in repos]
    try:
        contributed = contributed_repos(user, token)
        out["CONTRIBUTED"] = fmt(len(contributed))
    except Exception as exc:
        print(f"warning: contributed repos unavailable ({exc})", file=sys.stderr)
        contributed = []

    commits = adds = dels = 0
    complete = True
    targets = list(dict.fromkeys(own + contributed))  # de-duplicated
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(contributor_stats, r, user, token): r for r in targets}
        for fut, repo in futures.items():
            try:
                c, a, d = fut.result()
                commits, adds, dels = commits + c, adds + a, dels + d
            except Exception as exc:
                complete = False
                print(f"warning: {repo}: {exc}", file=sys.stderr)
    if complete or commits:
        out.update(COMMITS=fmt(commits), LOC=fmt(adds - dels), LOC_ADD=fmt(adds), LOC_DEL=fmt(dels))
    return out


def uptime(birthday: str | None, today: date) -> dict[str, str]:
    if not birthday:
        print("warning: ENV_BIRTHDAY not set", file=sys.stderr)
        return {"UPTIME": "?", "AGE": "?"}
    born = datetime.strptime(birthday.strip(), "%d.%m.%Y").date()

    def add_months(d: date, n: int) -> date:
        y, m = divmod(d.month - 1 + n, 12)
        y, m = d.year + y, m + 1
        return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))

    total = (today.year - born.year) * 12 + today.month - born.month
    if add_months(born, total) > today:
        total -= 1
    days = (today - add_months(born, total)).days
    years, months = divmod(total, 12)

    def unit(n: int, word: str) -> str:
        return f"{n} {word}{'' if n == 1 else 's'}"

    text = f"{unit(years, 'year')}, {unit(months, 'month')}, {unit(days, 'day')}"
    if months == 0 and days == 0:
        text += " 🎂"
    return {"UPTIME": text, "AGE": str(years)}


def row_lines(row: tuple) -> int:
    """Lines a row occupies in the table (sections and quotes get a blank line above)."""
    return 1 if row[0] == "kv" else 2


def parse_about(path: str, values: dict[str, str]) -> tuple[str, list[tuple]]:
    """Return (title, rows); rows are ("section", name), ("quote", text) or ("kv", key, value)."""
    title, rows = "", []
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    text = re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: values.get(m.group(1).upper(), m.group(0)), text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(">"):
            rows.append(("quote", clean(line.lstrip("> ").strip())))
        elif line.startswith("## "):
            rows.append(("section", line[3:].strip()))
        elif line.startswith("# "):
            title = line[2:].strip()
        elif line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 2 or set("".join(cells)) <= set("-: "):
                continue
            if cells[0].lower() in ("key", "") and cells[1].lower() in ("value", ""):
                continue
            rows.append(("kv", clean(cells[0]), clean(" | ".join(cells[1:]))))
        elif line[:2] in ("- ", "* "):
            body = line[2:]
            m = re.match(r"\*\*(.+?):?\*\*:?\s*(.*)", body) or re.match(r"([^:]+):\s*(.*)", body)
            if m:
                rows.append(("kv", clean(m.group(1)), clean(m.group(2))))
    return title, rows


def clean(s: str) -> str:
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)   # [text](url) -> text
    s = re.sub(r"[*_`]{1,2}([^*_`]+)[*_`]{1,2}", r"\1", s)
    return s.strip().rstrip(":")


# ---- ascii art --------------------------------------------------------------
def ascii_art(img: Image.Image, cols: int) -> tuple[int, list[list[tuple[str, tuple]]]]:
    img = img.convert("RGB")
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side))

    rows = max(1, round(cols * ART_CHAR_W / ART_LINE_H))
    small = img.resize((cols, rows), Image.Resampling.LANCZOS)
    color = ImageEnhance.Color(ImageEnhance.Brightness(small).enhance(ART_BRIGHTNESS)).enhance(1.15)

    keep = background_mask(small) if ART_REMOVE_BG else Image.new("L", small.size, 255)
    if ART_CIRCLE:
        keep = ImageChops.multiply(keep, circle_mask(small.size))

    # equalize contrast over the subject only, so facial details survive
    gray = ImageOps.grayscale(small).filter(ImageFilter.UnsharpMask(radius=1.5, percent=120))
    mask = keep.point(lambda v: 255 if v > 128 else 0)
    gray = ImageOps.equalize(gray, mask=mask if mask.getbbox() else None)

    grid = []
    for y in range(rows):
        line = []
        for x in range(cols):
            lum = (0.2 + 0.8 * gray.getpixel((x, y)) / 255) * keep.getpixel((x, y)) / 255
            ch = RAMP[min(len(RAMP) - 1, int(lum ** ART_GAMMA * len(RAMP)))]
            line.append((ch, color.getpixel((x, y))))
        grid.append(line)
    return rows, grid


def circle_mask(size: tuple[int, int], edge: float = 0.06) -> Image.Image:
    """Round crop in cell space; the outer `edge` fades out so the rim gets lighter blocks."""
    w, h = size
    mask = Image.new("L", size, 0)
    for y in range(h):
        for x in range(w):
            u, v = (x + 0.5) / w * 2 - 1, (y + 0.5) / h * 2 - 1
            fade = (1 - (u * u + v * v) ** 0.5) / edge
            mask.putpixel((x, y), int(255 * min(1.0, max(0.0, fade))))
    return mask


def background_mask(small: Image.Image) -> Image.Image:
    """Flood-fill from the border through similar colours (follows gradients).
    Returns an L mask: 0 = background, 255 = subject, soft one-pixel edge."""
    w, h = small.size
    px = small.load()
    border = [px[x, y] for x in range(w) for y in (0, h - 1)] + [px[x, y] for y in range(h) for x in (0, w - 1)]
    bg = tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))

    def dist(a, b):
        return sum((i - j) ** 2 for i, j in zip(a, b)) ** 0.5

    local_tol, global_tol = BG_KEY
    seen = set()
    stack = [(x, y) for x in range(w) for y in (0, h - 1)] + [(x, y) for y in range(h) for x in (0, w - 1)]
    stack = [p for p in stack if dist(px[p], bg) < global_tol]
    seen.update(stack)
    while stack:
        x, y = stack.pop()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in seen:
                if dist(px[nx, ny], px[x, y]) < local_tol and dist(px[nx, ny], bg) < global_tol:
                    seen.add((nx, ny))
                    stack.append((nx, ny))

    mask = Image.new("L", small.size, 255)
    for p in seen:
        mask.putpixel(p, 0)
    return mask.filter(ImageFilter.SMOOTH)


def quantize(rgb: tuple) -> str:
    r, g, b = (min(255, (c // 16) * 16 + 8) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


# ---- svg --------------------------------------------------------------------
def t(x: float, y: float, text: str, cls: str, fit: bool = True) -> str:
    length = f' textLength="{len(text) * CHAR_W:.1f}" lengthAdjust="spacingAndGlyphs"' if fit and text.isascii() else ""
    return f'<text x="{x:.1f}" y="{y:.1f}" class="{cls}"{length}>{escape(text)}</text>'


def value_text(x: float, y: float, value: str) -> str:
    """Value cell; "+1,234" is drawn green and "-1,234" red (git diff style)."""
    parts = re.split(r"([+-][\d,]+)", value)
    spans = "".join(
        f'<tspan class="{"add" if p[0] == "+" else "del"}">{escape(p)}</tspan>'
        if re.fullmatch(r"[+-][\d,]+", p) else escape(p)
        for p in parts if p
    )
    length = f' textLength="{len(value) * CHAR_W:.1f}" lengthAdjust="spacingAndGlyphs"' if value.isascii() else ""
    return f'<text x="{x:.1f}" y="{y:.1f}" class="value"{length}>{spans}</text>'


def render_svg(grid, mode: str, title: str, rows: list[tuple]) -> str:
    art_cols = len(grid[0])
    key_w = max((len(r[1]) for r in rows if r[0] == "kv"), default=8) + 5  # "key:" + " ... "
    val_w = max((len(r[2]) for r in rows if r[0] == "kv"), default=10)
    quote_w = max((len(r[1]) + 2 for r in rows if r[0] == "quote"), default=0)
    table_w = max(key_w + val_w, len(title), quote_w, 30)

    art_x = PAD
    tab_x = PAD + art_cols * ART_CHAR_W + GAP
    top = PAD + TITLEBAR

    table_h = ((2 if title else 0) + sum(row_lines(r) for r in rows)) * LINE_H
    art_h = len(grid) * ART_LINE_H
    content_h = max(table_h, art_h)
    art_top = top + (content_h - art_h) / 2
    tab_top = top + (content_h - table_h) / 2
    width = tab_x + table_w * CHAR_W + PAD
    height = top + content_h + PAD

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="{escape(title or "about")}">',
        "<style>",
        "text{font-family:ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace;"
        f"font-size:{FONT_SIZE}px;white-space:pre}}",
        *(f".{k}{{fill:{v}}}" for k, v in THEME.items() if k not in ("bg", "border")),
        ".title{font-weight:700}.section{font-weight:700}.quote{font-style:italic}",
        f".a{{font-size:{ART_FONT_SIZE}px;font-weight:700}}",
        "</style>",
        f'<rect x="0.5" y="0.5" width="{width - 1:.0f}" height="{height - 1:.0f}" rx="10" '
        f'fill="{THEME["bg"]}" stroke="{THEME["border"]}"/>',
        *(f'<circle cx="{20 + i * 18}" cy="18" r="5.5" fill="{c}"/>'
          for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840"))),
        '<g xml:space="preserve">',
    ]

    # left: ascii art (baseline offset ~ 0.8 line)
    for i, line in enumerate(grid):
        y = art_top + i * ART_LINE_H + ART_LINE_H * 0.8
        text = "".join(ch for ch, _ in line)
        if not text.strip():
            continue
        art_len = f'textLength="{len(text) * ART_CHAR_W:.1f}" lengthAdjust="spacingAndGlyphs"'
        if mode == "mono":
            out.append(f'<text x="{art_x:.1f}" y="{y:.1f}" class="a art" {art_len}>{escape(text)}</text>')
            continue
        spans, run, run_fill = [], "", None
        for ch, rgb in line:
            fill = quantize(rgb)
            if fill != run_fill and run:
                spans.append(f'<tspan fill="{run_fill}">{escape(run)}</tspan>')
                run = ""
            run, run_fill = run + ch, fill
        spans.append(f'<tspan fill="{run_fill}">{escape(run)}</tspan>')
        out.append(f'<text x="{art_x:.1f}" y="{y:.1f}" class="a" {art_len}>{"".join(spans)}</text>')

    # right: neofetch-style table
    line_no = 0

    def y_of(n: int) -> float:
        return tab_top + n * LINE_H + LINE_H * 0.8

    if title:
        out.append(t(tab_x, y_of(0), title, "title"))
        out.append(t(tab_x, y_of(1), "─" * table_w, "rule", fit=False))
        line_no = 2
    for row in rows:
        if row[0] == "quote":
            line_no += 1  # blank line before quote
            y = y_of(line_no)
            out.append(f'<rect x="{tab_x:.1f}" y="{y - FONT_SIZE + 1:.1f}" width="3" height="{LINE_H:.0f}" '
                       f'rx="1.5" class="qbar"/>')
            out.append(t(tab_x + 2 * CHAR_W, y, row[1], "quote", fit=False))
        elif row[0] == "section":
            line_no += 1  # blank line before section
            label = f"─ {row[1]} "
            out.append(t(tab_x, y_of(line_no), label, "section", fit=False))
            out.append(t(tab_x + len(label) * CHAR_W, y_of(line_no),
                         "─" * max(0, table_w - len(label)), "rule", fit=False))
        else:
            _, key, value = row
            head = f"{key}:"
            dots = " " + "." * (key_w - len(head) - 2) + " "
            out.append(t(tab_x, y_of(line_no), head, "key"))
            out.append(t(tab_x + len(head) * CHAR_W, y_of(line_no), dots, "dots"))
            out.append(value_text(tab_x + key_w * CHAR_W, y_of(line_no), value))
        line_no += 1

    out += ["</g>", "</svg>"]
    return "\n".join(out) + "\n"


# ---- main -------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", default=os.getenv("GITHUB_REPOSITORY_OWNER", "schmarante"))
    ap.add_argument("--about", default="about.md")
    ap.add_argument("--out", default="about.svg")
    ap.add_argument("--cols", type=int, default=0,
                    help="width of the ASCII portrait in characters (0 = match table height)")
    ap.add_argument("--mode", choices=("color", "mono"), default=os.getenv("ASCII_MODE", "color"))
    ap.add_argument("--avatar", help="use a local image instead of downloading the GitHub avatar")
    args = ap.parse_args()

    token = os.getenv("GITHUB_TOKEN")
    if args.avatar:
        img = Image.open(args.avatar)
    else:
        img = Image.open(io.BytesIO(http_get(f"https://github.com/{args.user}.png?size=400")))

    values = {
        **uptime(os.getenv("ENV_BIRTHDAY"), date.today()),
        **github_stats(args.user, token),
        "UPDATED": datetime.now(timezone.utc).strftime("%d.%m.%Y"),
    }
    title, rows = parse_about(args.about, values)
    cols = args.cols
    if cols <= 0:  # auto: portrait as tall as the table
        table_lines = (2 if title else 0) + sum(row_lines(r) for r in rows)
        cols = max(40, min(120, round(table_lines * LINE_H / ART_CHAR_W)))
    _, grid = ascii_art(img, cols)
    svg = render_svg(grid, args.mode, title, rows)

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(svg)
    print(f"wrote {args.out} ({len(svg) // 1024} KB, {len(grid)} art rows, {len(rows)} table rows)")


if __name__ == "__main__":
    main()
