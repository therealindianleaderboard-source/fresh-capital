#!/usr/bin/env python3
"""Refresh the Fresh Capital board from Gemini + Google Search grounding.

Rewrites only the DATA / SEEDS / BUILT blocks in index.html. Everything a
model returns has to survive validate() first, and a run that comes back
thin leaves the existing board alone rather than publishing a worse one.

    python update.py            # live run, needs GEMINI_API_KEY
    python update.py --selftest # offline check of parse/validate/merge/splice
"""
import datetime as dt
import json
import os
import pathlib
import re
import sys

HTML = pathlib.Path(__file__).parent / "index.html"

WINDOW_DAYS = 90        # both boards are a rolling 90-day window
MIN_MAJOR_M = 100       # majors floor, $M
MAX_MAJORS = 15
MAX_SEEDS = 12
KEEP_MAJORS = 6         # a run yielding fewer than this is treated as failed
KEEP_SEEDS = 4

# Free-tier Google Search grounding is 2.5-only (500 RPD, shared with Flash-Lite);
# the 3.x Flash models list grounding as unavailable on the free tier. Bump via env
# once that changes, or to move onto a paid tier.
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

REQUIRED = ("name", "sector", "amt", "stage", "date", "iso",
            "does", "hq", "what", "founders", "investors", "why", "src")

SCHEMA_DOC = """Return ONLY a JSON array. Each element must have exactly these keys:
  name       company name
  sector     one of: Foundation Models, AI Infrastructure, AI Coding, AI Applications,
             Chips/Compute, Data/Security, Robotics, AI Agents, Developer Tools
  amt        round size as a NUMBER in millions of USD (7400 = $7.4B, 2.5 = $2.5M)
  stage      "Series A".."Series F", "Seed", "Pre-seed", or "Private round"
  date       human date, e.g. "Sep 18, 2026"
  iso        announcement date as YYYY-MM-DD
  valuation  post-money if reported, else "Undisclosed"
  does       3-8 words for a dense list row, lowercase-ish
  hq         "City, Country"
  what       ONE plain sentence on what the company does. No marketing words.
  founders   names plus a one-line background each
  investors  the lead investors, comma separated
  why        2-3 sentences on what actually drove the round: ARR, customer counts,
             benchmark wins, capacity bookings, team pedigree. Concrete numbers.
  src        URL of a source that states the round size
  yc         YC batch like "YC S26" — include ONLY for actual YC batch companies

Every amount and date must come from the search results. Never estimate a number and
never invent a founder. If you cannot verify a round, leave it out."""


def prompt_majors(today, since):
    return f"""Today is {today}. Search the web for the LARGEST funding rounds announced by
AI, software and IT companies between {since} and {today}. Anything announced before
{since} is out, however large. Include rounds down to ${MIN_MAJOR_M}M — do not pad the
list with older rounds to make it longer.

Return the top {MAX_MAJORS}, sorted by amt descending.

{SCHEMA_DOC}"""


def prompt_seeds(today, since):
    return f"""Today is {today}. Search the web for the MOST RECENTLY announced pre-seed, seed
and first Series A rounds for small or new AI and software startups, between {since} and
{today}. Y Combinator batch companies especially. Nothing valued over $2B and no
household names — this list is the opposite end of the market from OpenAI and Anthropic.

Recency matters more than size: a $1.5M pre-seed from last week beats a $40M Series A
from two months ago. Return the {MAX_SEEDS} most recent, sorted by iso descending.

{SCHEMA_DOC}"""


def ask(prompt):
    """One grounded Gemini call. Returns raw text."""
    from google import genai

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    r = client.interactions.create(
        model=MODEL,
        input=prompt,
        tools=[{"type": "google_search"}],
    )
    return r.output_text


def parse_json(text):
    """Pull the first JSON array out of a model response."""
    text = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.M)
    start, depth = text.find("["), 0
    if start < 0:
        return []
    for i, c in enumerate(text[start:], start):
        depth += (c == "[") - (c == "]")
        if depth == 0:
            try:
                out = json.loads(text[start:i + 1])
            except json.JSONDecodeError:
                return []
            return out if isinstance(out, list) else []
    return []


def validate(entries, today, floor_m=0):
    """Drop anything unverifiable, misdated, or missing a field the page renders."""
    cutoff = today - dt.timedelta(days=WINDOW_DAYS)
    clean = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if any(not str(e.get(k, "")).strip() for k in REQUIRED):
            continue
        if not str(e["src"]).startswith("http"):
            continue
        try:
            iso = dt.date.fromisoformat(str(e["iso"]))
            amt = float(e["amt"])
        except (ValueError, TypeError):
            continue
        if not cutoff <= iso <= today:      # stale, or dated in the future
            continue
        if amt < floor_m or amt <= 0:
            continue
        e["amt"] = amt
        clean.append({k: e[k] for k in REQUIRED + ("valuation", "yc") if k in e})
    return clean


def merge(old, new):
    """New entries win on a name clash, but only with a newer iso."""
    by_name = {e["name"].strip().lower(): e for e in old}
    for e in new:
        k = e["name"].strip().lower()
        if k not in by_name or e["iso"] >= by_name[k]["iso"]:
            by_name[k] = e
    return list(by_name.values())


def read_block(html, tag):
    m = re.search(rf"/\*{tag}_START\*/\s*const \w+ = (.*?);\s*/\*{tag}_END\*/", html, re.S)
    return json.loads(m.group(1)) if m else []


def write_block(html, tag, const, value):
    body = f"/*{tag}_START*/\nconst {const} = {json.dumps(value, indent=0, ensure_ascii=False)};\n/*{tag}_END*/"
    return re.sub(rf"/\*{tag}_START\*/.*?/\*{tag}_END\*/", lambda _: body, html, flags=re.S)


def main():
    today = dt.date.today()
    since = today - dt.timedelta(days=WINDOW_DAYS)
    html = HTML.read_text()

    majors = validate(read_block(html, "DATA"), today, MIN_MAJOR_M)
    seeds = validate(read_block(html, "SEEDS"), today)
    print(f"kept from existing board: {len(majors)} majors, {len(seeds)} seeds")

    majors = merge(majors, validate(parse_json(ask(prompt_majors(today, since))), today, MIN_MAJOR_M))
    seeds = merge(seeds, validate(parse_json(ask(prompt_seeds(today, since))), today))

    majors.sort(key=lambda e: -e["amt"])
    seeds.sort(key=lambda e: e["iso"], reverse=True)
    majors, seeds = majors[:MAX_MAJORS], seeds[:MAX_SEEDS]
    print(f"after merge: {len(majors)} majors, {len(seeds)} seeds")

    if len(majors) < KEEP_MAJORS or len(seeds) < KEEP_SEEDS:
        sys.exit(f"refusing to write a thin board ({len(majors)}/{len(seeds)}); "
                 "left index.html untouched")

    out = write_block(html, "DATA", "DATA", majors)
    out = write_block(out, "SEEDS", "SEEDS", seeds)
    out = write_block(out, "BUILT", "BUILT", str(today))
    if out == html:
        print("no change")
        return
    HTML.write_text(out)
    print(f"wrote index.html — {len(majors)} majors, {len(seeds)} seeds, built {today}")


def selftest():
    today = dt.date(2026, 9, 27)
    good = {k: "x" for k in REQUIRED}
    good |= {"name": "Real Co", "amt": 400, "iso": "2026-09-01", "src": "https://e.com/a"}

    assert parse_json('```json\n[{"a":1}]\n```') == [{"a": 1}]
    assert parse_json("blah [1, [2]] trailing") == [1, [2]]
    assert parse_json("not json at all") == []
    assert parse_json('[{"a": broken}]') == []          # malformed, not a crash

    assert len(validate([good], today, 100)) == 1
    assert validate([good], today, 500) == []                                  # under floor
    assert validate([good | {"iso": "2026-01-01"}], today, 100) == []           # stale
    assert validate([good | {"iso": "2026-12-01"}], today, 100) == []           # future
    assert validate([good | {"src": "ask-me"}], today, 100) == []               # no source
    assert validate([good | {"why": "  "}], today, 100) == []                   # blank field
    assert validate([{k: v for k, v in good.items() if k != "founders"}], today, 100) == []
    assert validate([good | {"amt": "lots"}], today, 100) == []                 # non-numeric

    old = [good, good | {"name": "Only Old", "iso": "2026-08-01"}]
    fresh = [good | {"amt": 900, "iso": "2026-09-20"}, good | {"name": "Brand New"}]
    m = merge(old, fresh)
    assert len(m) == 3, m                                   # Real Co merged, not duplicated
    assert next(e for e in m if e["name"] == "Real Co")["amt"] == 900    # newer iso wins
    assert merge(old, [good | {"amt": 1, "iso": "2026-01-01"}])[0]["amt"] == 400  # older loses

    html = HTML.read_text()
    assert len(read_block(html, "DATA")) > 0 and len(read_block(html, "SEEDS")) > 0
    spliced = write_block(html, "SEEDS", "SEEDS", [good])
    assert read_block(spliced, "SEEDS") == [good]
    assert read_block(spliced, "DATA") == read_block(html, "DATA")   # untouched
    assert len(spliced) < len(html) and spliced.count("<title>") == 1
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
