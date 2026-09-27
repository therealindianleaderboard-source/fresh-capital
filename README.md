# Fresh Capital

A leaderboard of the biggest AI and software funding rounds of the last 90 days, plus the
newest pre-seed and seed rounds. One static page, no build step, no database — the data
lives in two arrays inside `index.html` and a daily GitHub Action rewrites them.

## How it updates itself

`update.py` runs once a day:

1. Reads the two arrays already in `index.html` and drops anything now older than 90 days.
2. Asks Gemini, with Google Search grounding, for new rounds in the window.
3. **Validates every returned entry** — a real source URL, a parseable date inside the
   window, a numeric amount above the floor, no blank fields. Anything else is dropped.
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

## Why Gemini 2.5 Flash and not 3.x

Free-tier Google Search grounding is available on `gemini-2.5-flash` and
`gemini-2.5-flash-lite` only — 500 grounded requests per day, shared between them. The 3.x
Flash models list grounding as *not available* on the free tier. This job makes two grounded
calls a day, so it sits far under the cap.

Override with the `GEMINI_MODEL` env var if that changes or you move to a paid tier.

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
| `MIN_MAJOR_M` | floor for the majors board, in $M (100) |
| `MAX_MAJORS` / `MAX_SEEDS` | rows per board (15 / 12) |
| `KEEP_MAJORS` / `KEEP_SEEDS` | below this, a run is treated as failed (6 / 4) |

## A caveat worth keeping

Grounded search will occasionally return a wrong date or an unverifiable amount. The
validation gate catches the mechanical failures — missing sources, bad dates, stale rounds —
but it cannot catch a plausible-looking wrong number. Spot-check the diff on a commit now
and then; each entry links its source in the popup.
