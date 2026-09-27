# Fresh Capital

A leaderboard of the biggest AI and software funding rounds of the last 90 days, plus the
newest pre-seed and seed rounds. One static page, no build step, no database — the data
lives in two arrays inside `index.html` and a daily GitHub Action rewrites them.

## How it updates itself

`update.py` runs once a day:

1. Reads the two arrays already in `index.html` and drops anything now older than 90 days.
2. Pulls four funding-news RSS feeds, keeps the items that look like funding news and fall
   inside the window, and fetches the article text for the newest ~22 of them.
3. Sends that text to Gemini and asks it to extract structured entries **from those items
   only**, then **validates every one** — the source URL must be one we actually handed it,
   the date must parse and sit inside the window, the amount must be a positive number, and
   no rendered field may be blank. Anything else is dropped.
4. Merges, sorts (majors by size, seeds by date), and splices the arrays back in.
5. Refuses to write a thin board. Fewer than 6 majors or 4 seeds means the run failed, and
   `index.html` is left exactly as it was rather than replaced with something worse.

Only the `DATA`, `SEEDS` and `BUILT` blocks are ever touched. The page itself is hand-written.

## Setup

**1. Gemini API key** — free at [aistudio.google.com](https://aistudio.google.com/apikey), then:

```bash
gh secret set GEMINI_API_KEY        # paste the key when prompted
```

**2. GitHub Pages** — Settings → Pages → Source: *Deploy from a branch*, branch `main`, folder `/`.

That's it. The Action runs at 11:00 UTC daily, and Pages redeploys on each commit.

## Why RSS instead of Google Search grounding

Grounding would be less code, but it is not reachable on the Gemini free tier. A grounded
call returns `429 quota exceeded` on a brand-new key while the identical call without tools
succeeds — the free tier's grounded-search quota is zero. (`gemini-2.5-flash` did carry free
grounding at 500 requests/day, but it is now closed to new API keys.)

So this fetches the news itself and uses Gemini for extraction only, which is a plain text
call and free. One call per day, against a free-tier limit of roughly 20 requests/day.

Override the model with the `GEMINI_MODEL` env var. If you ever move to a paid tier,
grounded search is about $14 per 1,000 searches.

## Running it by hand

```bash
python update.py --selftest        # offline: parse, validate, merge, splice
GEMINI_API_KEY=... python update.py
```

Or hit **Actions → Daily board update → Run workflow** on GitHub.

## Tuning

Knobs at the top of `update.py`:

| | |
|---|---|
| `WINDOW_DAYS` | how far back a round stays on the board (90) |
| `MIN_MAJOR_M` | the split, in $M: at or above is a major, below is a seed (100) |
| `FEEDS` | the RSS feeds scanned each run |
| `MAX_ARTICLES` | how many article bodies get fetched and sent to the model (22) |
| `MAX_MAJORS` / `MAX_SEEDS` | rows per board (15 / 12) |
| `KEEP_MAJORS` / `KEEP_SEEDS` | below this, a run is treated as failed (6 / 4) |

## A caveat worth keeping

The model can still misread an amount or a founder's name out of an article it was given.
The validation gate catches the mechanical failures — fabricated sources, bad dates, stale
rounds, blank fields — but not a plausible-looking wrong number. Spot-check the diff on a
commit now and then; every entry links its source in the popup.

Feeds also go stale or start blocking. `eu-startups.com` and `finsmes.com` both 403 a plain
urllib request, which is why they aren't in `FEEDS`. If the candidate pool shrinks, the run
fails safe rather than publishing a thin board.
