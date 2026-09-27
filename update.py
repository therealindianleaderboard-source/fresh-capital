#!/usr/bin/env python3
"""Refresh the Fresh Capital board: funding-news RSS -> Gemini -> index.html.

We do the retrieval ourselves and use Gemini only to extract structured entries
from text we hand it. Google Search grounding would be tidier, but it is not
available on the Gemini free tier (a grounded call returns 429 on a zero quota,
while the same call without tools succeeds), and plain calls are free.

Fetching it ourselves has a side benefit: `src` is the feed's own link, so a
source URL cannot be invented.

    python update.py            # live run, needs GEMINI_API_KEY
    python update.py --selftest # offline check of the parsing and splice logic
"""
import datetime as dt
import email.utils
import html
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HTML = pathlib.Path(__file__).parent / "index.html"

FEEDS = [
    "https://techcrunch.com/category/venture/feed/",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://news.crunchbase.com/feed/",
    "https://tech.eu/feed/",
]

WINDOW_DAYS = 90        # both boards are a rolling 90-day window
MIN_MAJOR_M = 100       # at or above this a round is a "major", below it a "seed"
MAX_MAJORS = 15
MAX_SEEDS = 12
KEEP_MAJORS = 6         # a run yielding fewer than this is treated as failed
KEEP_SEEDS = 4
MAX_ARTICLES = 14       # articles whose body we fetch and send to the model
BODY_CHARS = 6000       # site nav eats the first ~1k chars, so leave room for the article
# Several funding-news sites 403 anything that self-identifies as a bot.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0.0.0 Safari/537.36")

# Free-tier grounding is unavailable, so no tools are used and any text model works.
# The flagship flash model gets demand-throttled (503); the lite models are quieter
# and carry a larger free daily request allowance, so they stand in when it is busy.
MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.8-flash"
).split(",") if m.strip()]

FUNDING = re.compile(
    r"\b(raise[sd]?|raising|secure[sd]|closes?|lands?|nets?|bags?)\b|"
    r"\b(seed|pre-seed|series\s+[a-f])\b|\bfunding\b|\b[$€£]\d",
    re.I)

# Facts that must be right or the entry is worthless. No amount, no date, no source,
# no entry.
HARD = ("name", "sector", "amt", "stage", "iso", "does", "what", "why", "src")
# Details a short news item often omits. Saying so is honest and still useful, and
# beats dropping a real round over a missing city.
SOFT = {"founders": "Not disclosed", "hq": "Not disclosed",
        "investors": "Not disclosed", "valuation": "Undisclosed"}
REQUIRED = HARD + tuple(SOFT)


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/xml, text/html;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        print(f"  fetch failed {url}: {e}")
        return ""


def strip_html(raw):
    raw = re.sub(r"<(script|style)\b.*?</\1>", " ", raw, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw))).strip()


def feed_items(since, today):
    """Funding-looking items from every feed, inside the window, newest first."""
    items = []
    for url in FEEDS:
        body = fetch(url)
        if not body:
            continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            print(f"  bad xml {url}: {e}")
            continue
        found = 0
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            pub = (it.findtext("pubDate") or "").strip()
            desc = strip_html(it.findtext("description") or "")[:600]
            if not (title and link.startswith("http")):
                continue
            try:
                date = email.utils.parsedate_to_datetime(pub).date()
            except (TypeError, ValueError):
                continue
            if not since <= date <= today:
                continue
            if not FUNDING.search(title + " " + desc):
                continue
            items.append({"title": title, "link": link, "date": date.isoformat(),
                          "desc": desc})
            found += 1
        print(f"  {found} funding items from {urllib.parse.urlsplit(url).netloc or url}")
    items.sort(key=lambda i: i["date"], reverse=True)
    seen, out = set(), []
    for i in items:
        if i["link"] not in seen:
            seen.add(i["link"])
            out.append(i)
    return out


def with_bodies(items):
    """Pull article text so the model has something real to write 'why' from."""
    for i in items:
        i["body"] = strip_html(fetch(i["link"]))[:BODY_CHARS]
    return items


def build_prompt(items, today):
    blocks = "\n\n".join(
        f"### ITEM {n}\nURL: {i['link']}\nDATE: {i['date']}\nHEADLINE: {i['title']}\n"
        f"{i.get('body') or i['desc']}"
        for n, i in enumerate(items, 1))
    return f"""Today is {today}. Below are {len(items)} news items about startup funding.

Extract every item that announces a funding round raised by an AI, software or IT
company. Skip everything else: acquisitions, fund launches by VC firms, IPOs, layoffs,
product launches, and rounds raised by companies that are not software or AI.

Use ONLY the text below - do not add companies you happen to know about from elsewhere.
`src` must be that item's URL exactly as given, and `iso` must be that item's DATE.

Leave a company out only if the text does not make clear its name, its round size, or
what it does. For any OTHER field the item simply does not mention - founders, city,
investors, valuation - write "Not disclosed". Never invent a founder, an investor or a
number, and never drop a real round just because a detail is missing.

Return ONLY a JSON array. Each element has exactly these keys:
  name       company name
  sector     one of: Foundation Models, AI Infrastructure, AI Coding, AI Applications,
             Chips/Compute, Data/Security, Robotics, AI Agents, Developer Tools
  amt        round size as a NUMBER in millions of USD (7400 = 7.4 billion, 2.5 = 2.5
             million). Convert other currencies to USD at a rough current rate.
  stage      "Series A".."Series F", "Seed", "Pre-seed", or "Private round"
  iso        the item's DATE, as YYYY-MM-DD
  valuation  post-money if the item states one, else "Undisclosed"
  does       3-8 words for a dense list row, lowercase-ish, e.g. "checkout plumbing for AI sellers"
  hq         "City, Country"
  what       ONE plain sentence on what the company does. No marketing words.
  founders   names plus a one-line background each, if the item names them
  investors  the lead investors, comma separated
  why        2-3 sentences on what actually drove the round, using concrete facts from
             the item: ARR, customer counts, benchmark results, capacity booked, team
             pedigree. If the item gives no traction numbers, say plainly that the round
             rests on team and thesis.
  yc         a YC batch like "YC S26", ONLY if the item says the company is a YC company

{blocks}"""


def transient(msg):
    """Worth waiting out, as opposed to a wrong model or a bad key."""
    return any(t in msg.lower() for t in
               ("503", "429", "high demand", "overloaded", "unavailable",
                "quota", "rate limit", "timeout", "deadline"))


def ask(prompt, tries=3):
    """First model that answers wins. Backs off on load, moves on for anything else."""
    from google import genai
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    last = "no models configured"
    for model in MODELS:
        for attempt in range(tries):
            try:
                return client.interactions.create(model=model, input=prompt).output_text
            except Exception as e:                  # SDK error types are nested and vary
                last = f"{model}: {type(e).__name__}: {str(e)[:160]}"
                print(f"  {last}")
                if not transient(str(e)):
                    break                           # bad model or bad key, try the next
                if attempt < tries - 1:
                    time.sleep(8 * (attempt + 1))
    sys.exit(f"every model failed, board left untouched - last error: {last}")


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


def validate(entries, today, allowed_src=None):
    """Drop anything unverifiable, misdated, or missing a field the page renders."""
    cutoff = today - dt.timedelta(days=WINDOW_DAYS)
    clean = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if any(not str(e.get(k, "")).strip() for k in HARD):
            continue
        if not str(e["src"]).startswith("http"):
            continue
        if allowed_src is not None and e["src"] not in allowed_src:
            continue          # a source we never handed it is a fabricated one
        try:
            iso = dt.date.fromisoformat(str(e["iso"]))
            amt = float(e["amt"])
        except (ValueError, TypeError):
            continue
        if not cutoff <= iso <= today:      # stale, or dated in the future
            continue
        if amt <= 0:
            continue
        e["amt"] = amt
        e["date"] = f"{iso:%b} {iso.day}, {iso.year}"   # derived, so it always matches iso
        for k, default in SOFT.items():
            if not str(e.get(k, "")).strip():
                e[k] = default
        clean.append({k: e[k] for k in REQUIRED + ("date", "yc") if k in e})
    return clean


def merge(old, new):
    """New entries win on a name clash, but only with a newer iso."""
    by_name = {e["name"].strip().lower(): e for e in old}
    for e in new:
        k = e["name"].strip().lower()
        if k not in by_name or e["iso"] >= by_name[k]["iso"]:
            by_name[k] = e
    return list(by_name.values())


def read_block(page, tag):
    m = re.search(rf"/\*{tag}_START\*/\s*const \w+ = (.*?);\s*/\*{tag}_END\*/", page, re.S)
    return json.loads(m.group(1)) if m else []


def write_block(page, tag, const, value):
    body = (f"/*{tag}_START*/\nconst {const} = "
            f"{json.dumps(value, indent=0, ensure_ascii=False)};\n/*{tag}_END*/")
    return re.sub(rf"/\*{tag}_START\*/.*?/\*{tag}_END\*/", lambda _: body, page, flags=re.S)


def main():
    today = dt.date.today()
    since = today - dt.timedelta(days=WINDOW_DAYS)
    page = HTML.read_text()

    majors = validate(read_block(page, "DATA"), today)
    seeds = validate(read_block(page, "SEEDS"), today)
    print(f"kept from existing board: {len(majors)} majors, {len(seeds)} seeds")

    print("reading feeds...")
    items = feed_items(since, today)
    print(f"{len(items)} candidate items; fetching {min(len(items), MAX_ARTICLES)} articles")
    items = with_bodies(items[:MAX_ARTICLES])

    found = []
    if items:
        raw = ask(build_prompt(items, today))
        parsed = parse_json(raw)
        found = validate(parsed, today, allowed_src={i["link"] for i in items})
        print(f"model returned {len(raw)} chars, {len(parsed)} objects, "
              f"{len(found)} survived validation")
        if not found:
            print("  raw head: " + raw[:400].replace("\n", " "))
            for e in parsed[:4]:
                missing = [k for k in REQUIRED if not str(e.get(k, "")).strip()]
                print(f"  dropped {e.get('name')!r}: iso={e.get('iso')} "
                      f"amt={e.get('amt')!r} src={str(e.get('src'))[:60]!r} missing={missing}")

    majors = merge(majors, [e for e in found if e["amt"] >= MIN_MAJOR_M])
    seeds = merge(seeds, [e for e in found if e["amt"] < MIN_MAJOR_M])
    majors.sort(key=lambda e: -e["amt"])
    seeds.sort(key=lambda e: e["iso"], reverse=True)
    majors, seeds = majors[:MAX_MAJORS], seeds[:MAX_SEEDS]
    print(f"after merge: {len(majors)} majors, {len(seeds)} seeds")

    if len(majors) < KEEP_MAJORS or len(seeds) < KEEP_SEEDS:
        sys.exit(f"refusing to write a thin board ({len(majors)}/{len(seeds)}); "
                 "left index.html untouched")

    out = write_block(page, "DATA", "DATA", majors)
    out = write_block(out, "SEEDS", "SEEDS", seeds)
    out = write_block(out, "BUILT", "BUILT", str(today))
    if out == page:
        print("no change")
        return
    HTML.write_text(out)
    print(f"wrote index.html - {len(majors)} majors, {len(seeds)} seeds, built {today}")


def selftest():
    today = dt.date(2026, 9, 27)
    good = {k: "x" for k in HARD}
    good |= {"name": "Real Co", "amt": 400, "iso": "2026-09-01", "src": "https://e.com/a"}

    assert parse_json('```json\n[{"a":1}]\n```') == [{"a": 1}]
    assert parse_json("blah [1, [2]] trailing") == [1, [2]]
    assert parse_json("not json at all") == []
    assert parse_json('[{"a": broken}]') == []          # malformed, not a crash

    v = validate([good], today)
    assert len(v) == 1
    assert v[0]["date"] == "Sep 1, 2026"                  # derived from iso, not trusted
    assert v[0]["founders"] == "Not disclosed"            # soft field filled, not dropped
    assert v[0]["valuation"] == "Undisclosed"
    assert validate([good | {"what": " "}], today) == []   # a hard field still drops it
    assert validate([good | {"iso": "2026-01-01"}], today) == []        # stale
    assert validate([good | {"iso": "2026-12-01"}], today) == []        # future
    assert validate([good | {"src": "ask-me"}], today) == []            # not a URL
    assert validate([good | {"why": "  "}], today) == []                # blank hard field
    assert len(validate([{k: v for k, v in good.items() if k != "founders"}], today)) == 1
    assert validate([good | {"amt": "lots"}], today) == []              # non-numeric
    assert validate([good | {"amt": 0}], today) == []
    assert validate([good], today, allowed_src={"https://other"}) == []   # unseen source
    assert len(validate([good], today, allowed_src={"https://e.com/a"})) == 1

    old = [good, good | {"name": "Only Old", "iso": "2026-08-01"}]
    fresh = [good | {"amt": 900, "iso": "2026-09-20"}, good | {"name": "Brand New"}]
    m = merge(old, fresh)
    assert len(m) == 3, m                                   # merged, not duplicated
    assert next(e for e in m if e["name"] == "Real Co")["amt"] == 900   # newer iso wins
    assert merge(old, [good | {"amt": 1, "iso": "2026-01-01"}])[0]["amt"] == 400

    assert transient("Error code: 503 - high demand") and transient("429 quota")
    assert not transient("404 model not found") and not transient("invalid api key")

    assert strip_html("<p>a <b>b</b><script>junk()</script></p>&amp;") == "a b &"
    assert FUNDING.search("Acme raises $4M seed") and not FUNDING.search("Acme hires a CFO")

    rss = """<rss><channel>
      <item><title>Acme raises $4M seed</title><link>https://e.com/1</link>
        <pubDate>Fri, 25 Sep 2026 10:00:00 +0000</pubDate>
        <description>&lt;p&gt;Acme, an AI firm&lt;/p&gt;</description></item>
      <item><title>Beta hires a new CFO</title><link>https://e.com/2</link>
        <pubDate>Fri, 25 Sep 2026 10:00:00 +0000</pubDate><description>x</description></item>
      <item><title>Gamma raises $9M</title><link>https://e.com/3</link>
        <pubDate>Mon, 06 Jan 2025 10:00:00 +0000</pubDate><description>x</description></item>
      <item><title>Delta raises $1M</title><link>notaurl</link>
        <pubDate>Fri, 25 Sep 2026 10:00:00 +0000</pubDate><description>x</description></item>
    </channel></rss>"""
    saved_feeds, saved_fetch = FEEDS, globals()["fetch"]
    globals()["FEEDS"] = ["mock"]
    globals()["fetch"] = lambda u, timeout=20: rss
    try:
        got = feed_items(today - dt.timedelta(days=WINDOW_DAYS), today)
    finally:
        globals()["FEEDS"], globals()["fetch"] = saved_feeds, saved_fetch
    # keeps only the item that is funding news, in-window, and has a real link
    assert [i["link"] for i in got] == ["https://e.com/1"], got
    assert got[0]["desc"] == "Acme, an AI firm"

    page = HTML.read_text()
    assert len(read_block(page, "DATA")) > 0 and len(read_block(page, "SEEDS")) > 0
    spliced = write_block(page, "SEEDS", "SEEDS", [good])
    assert read_block(spliced, "SEEDS") == [good]
    assert read_block(spliced, "DATA") == read_block(page, "DATA")   # untouched
    assert spliced.count("<title>") == 1
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
