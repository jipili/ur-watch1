# UR賃貸 空室ウォッチャー (UR Chintai vacancy watcher)

Checks every UR Chintai property in Tokyo (308) and Kanagawa (166) — 474
buildings total — against UR's own internal JSON API every 15 minutes,
and emails you the moment a room becomes newly vacant.

This isn't scraping HTML: it calls the same backend endpoint UR's own
website calls (`detail_bukken_room`), so it's fast and reliable. Credit
to https://duongnt.com/urchintai-api/ for reverse-engineering that API.

## How it works

- `ur_properties.json` — every Tokyo/Kanagawa property, with the
  `shisya`/`danchi`/`shikibetu` codes UR uses internally.
- `check_ur.py` — for each property, asks "any vacant rooms?", compares
  the vacant room IDs to last run's list (`state.json`), and emails you
  about anything new.
- `property_static.json` — a cache of address / nearest station / pet
  policy per property. This data never changes, so it's only fetched
  once per property (the first time that property ever shows a new
  vacancy) instead of on every 15-minute run.
- `.github/workflows/ur-watch.yml` — runs `check_ur.py` on a schedule via
  GitHub Actions (free for public repos), and commits `state.json` +
  `property_static.json` back so the next run knows what's already been
  seen and doesn't re-fetch static info it already has.

## What's in each email

For every newly-vacant room:
- Property name and prefecture
- Address
- Nearest station (with walk time, as UR lists it)
- Pet policy (best-effort — see note below)
- Room type (1LDK, 2DK, etc.)
- Size (m²)
- Rent (+ common fee)
- Floor
- Direct link to the listing page

**A note on address / station / pet policy accuracy:** the vacancy
endpoint this script relies on for room data (`detail_bukken_room`) is
well-documented by a third party (see Credit below). The endpoints used
for address/station/pet info are *not* publicly documented — UR doesn't
publish a field reference for them — so `check_ur.py` parses those
responses with best-effort heuristics (looking for keys/values that look
like an address, a station name with a walk time, or the text "ペット可").
This works correctly for the properties I spot-checked, but if a field
ever can't be confidently extracted, the email prints "(see listing
link)" instead of guessing — and the listing link is always accurate, so
you can confirm any field there in one click. If you notice a
consistently wrong or missing field for your area, the extraction logic
is the `extract_address` / `extract_station` / `extract_pet_friendly`
functions near the top of `check_ur.py` — easy to adjust once you see
what UR's actual response looks like for a property near you (the
script prints the raw error to the Actions log if a request fails, and
you can temporarily add a `print(combined)` inside `fetch_property_static`
to see the raw JSON on a real run).

## Setup (10 minutes)

1. **Create a new GitHub repo** (public, so Actions minutes are free) and
   push these files to it.

2. **Get a Gmail app password** (or use any other SMTP account):
   - Go to https://myaccount.google.com/apppasswords
   - Create an app password for "Mail"
   - Copy the 16-character password

3. **Add repo secrets** — Settings → Secrets and variables → Actions →
   New repository secret:
   | Name | Value |
   |---|---|
   | `SMTP_HOST` | `smtp.gmail.com` |
   | `SMTP_PORT` | `587` |
   | `SMTP_USER` | your Gmail address |
   | `SMTP_PASS` | the app password from step 2 |
   | `NOTIFY_TO` | where you want the alert sent (can be same as SMTP_USER) |

4. **Enable Actions** on the repo if prompted, then trigger a first run
   manually: Actions tab → "UR vacancy watch" → Run workflow. Check the
   logs — it should say something like `Checked 474/474 properties`.

5. Leave it running. It checks every 15 minutes from then on.

## Adjusting what you're notified about

Open `check_ur.py` and edit the top of the file:

```python
MADORI_WHITELIST = ["1LDK", "2DK", "2LDK"]   # only these layouts
MAX_RENT_YEN = 150000                         # only rooms ≤ ¥150,000/month
```

Leave a list empty / value `None` to disable that filter.

## Adjusting the check frequency

Edit the `cron` line in `.github/workflows/ur-watch.yml`. `*/15 * * * *`
means every 15 minutes. `*/5 * * * *` means every 5 minutes (GitHub's
practical minimum). Faster checks mean you hear about a vacancy sooner,
but also mean more requests to UR's servers — 15 minutes is already fast
enough to beat almost anyone checking by hand, and matches what the paid
third-party notification services advertise.

## Notes

- The first run will treat every currently-vacant room as "already
  known" (it has no prior state to compare against), so you won't get a
  flood of emails for existing vacancies — only for rooms that open up
  *after* that first run. If you want a one-time snapshot of what's
  vacant right now, run `check_ur.py` locally first and read `state.json`
  and the console output, or just browse the site once before turning
  this on.
- Public GitHub repos get unlimited Actions minutes; if you make the
  repo private, GitHub's free tier caps you around 2,000 minutes/month,
  which is still comfortable at a 15-minute interval (~40s/run × ~2,900
  runs/month ≈ 30 minutes/month).
- This uses UR's own public-facing API, the same one their website's
  JavaScript calls — it's not bypassing any login or paywall, just
  polling politely (0.4s delay between each of the 474 requests, once
  per run) instead of a human refreshing the page.
