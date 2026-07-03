# ZwiftRacing to Discord (ECRO)

Scrapes **ECRO World Tour** race results from [zwiftracing.app](https://www.zwiftracing.app)
and posts team finishers to a Discord channel via webhook.

Example post:

```
ECRO World Tour | Chasing Suisse - Stage 4
📅 Friday, June 19, 2026 11:30 PM (adjusts to your timezone)
🗺️ Quatch Quest   46.8 km   1706 m
🔗 https://www.zwiftracing.app/events/5609317

Rank 3 🥉 🟨 [D] Em Kullman USMeS (USMeS) — 2:02:25
+1:51 — 3.2 w/kg
Rank 4 🟨 [D] Aaron Keirn USMeS (USMeS) — 2:13:31
+12:58 — 2.7 w/kg
```

## Features

- **Automated login** — zwiftracing.app authenticates with NextAuth (Google or
  Strava OAuth). The script keeps a persistent Microsoft Edge profile so the
  Google session survives between runs; after a one-time manual login, future
  sign-ins are fully automatic (login state is verified via `/api/auth/session`).
- **All pens scraped** — each event page shows pens A–E as tabs and only loads
  pen A by default; the script clicks through every pen and tags riders with
  their pen letter.
- **Rich Discord format** — rank with medals, pen color emoji, finish time,
  gap to winner, avg w/kg, and a `<t:…:F>` timestamp that renders in each
  viewer's local timezone.
- **No duplicate posts** — posted event IDs are remembered in a local file.

## Requirements

- Windows with Microsoft Edge installed
- Python 3.10+
- `pip install playwright python-dotenv requests`

## Setup

1. Copy `.env.example` to `.env` and fill in your Discord webhook URL
   (and optionally your Google account email).
2. Run the script: `python "ZwiftRacing to Discord (ECRO).py"`
3. On the first run an Edge window opens — log in to zwiftracing.app with
   Google or Strava once. The session is stored in the local `edge_profile/`
   folder and reused automatically afterwards.

## Configuration

| Setting | Where | Purpose |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | `.env` | Discord webhook to post results to |
| `GOOGLE_EMAIL` | `.env` | Picks your account on Google's account chooser |
| `GOOGLE_PASSWORD` | `.env` (optional) | Last-resort automated login; usually unnecessary |
| `EVENT_QUERY` | script | Event title filter (default `ECRO`) |
| `TEAM_FILTER` | script | Team name to report (default `usmes`) |
| `LOCAL_TZ` | script | Timezone the event dates are parsed in |
| `MAX_EVENTS` | environment variable | Only scrape the N most recent events (0/unset = all) |

## Security notes

Never commit `.env` (webhook URL) or `edge_profile/` (live login session
cookies) — both are covered by `.gitignore`.
