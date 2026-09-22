
# WNBA Prop Analyzer

A local browser-based prop analyzer that pulls data directly from the official WNBA Stats service (`stats.wnba.com`).

## What it does

- Loads WNBA players and groups them by team.
- Lets you choose:
  - season
  - team
  - player
  - opponent
  - market: points, rebounds, assists, or 3-pointers made
  - prop line
- Pulls the player's official game log.
- Uses only August + September games for the recent sample.
- Calculates:
  - exact L10 values and hit rate
  - exact L5 values and hit rate
  - current-season H2H values and hit rate
  - season average
  - season minutes
- official WNBA Season Leaders rank/cross-check for the selected category
- Applies the strict rule:
  - 10 games: at least 9/10
  - 9 games: at least 8/9
  - 8 games: at least 7/8
  - L5: at least 4/5
  - every available current-season H2H must clear the line
- Returns:
  - 🔥 STATISTICAL PASS
  - 🟡 CLOSE — H2H DOES NOT FULLY PASS
  - ❌ FAIL

## Important

The official WNBA Stats API does **not** provide a live injury report. The app therefore calls a result a **STATISTICAL PASS**, not a final injury-cleared HOT pick. Injury/availability can be added as a second data source later.

## Windows setup

1. Install Python 3.11+ from python.org.
2. Open Command Prompt inside this folder.
3. Run:

   py -m pip install -r requirements.txt

4. Then run:

   py app.py

5. Open:

   http://127.0.0.1:5000

You can also double-click `start_windows.bat` after dependencies are installed.

## Official endpoints used

- `https://stats.wnba.com/stats/leaguedashplayerstats`
- `https://stats.wnba.com/stats/playergamelogs`
- `https://stats.wnba.com/stats/leagueleaders` (backs the Season Leaders view)

WNBA uses LeagueID `10`.

## Notes about WNBA Stats

`stats.wnba.com` can occasionally throttle or block automated requests. The backend uses browser-like headers, caching, timeouts, and retries, but upstream availability is still outside the app's control.


## v3 fix

The official WNBA `playergamelogs` feed can return `GAME_DATE` as an ISO timestamp
such as `2026-09-20T00:00:00`. Earlier versions only recognized a few plain-date
formats, which could make the August/September Last 10 and Last 5 sections appear
empty even though H2H data was present. v3 accepts ISO timestamps and the common
WNBA date formats.


## v4 UI upgrade

- Modern dark WNBA-style dashboard
- Official team logos in the team/opponent selectors and matchup preview
- Official player headshot on analysis results when the WNBA/NBA CDN has one
- Graceful initials fallback if a headshot is unavailable
- Team logo in the player result card
- Redesigned result metrics, hit/miss values, detailed tables, mobile layout
- No local logo/image bundle is required; imagery is requested from the official CDN


## v5 player-photo fix

The team-logo CDN worked when loaded directly in the browser, but WNBA player
headshots can reject or fail direct requests from a localhost page. v5 adds a local
Flask image proxy at `/api/player-image/<player_id>`. The backend requests the
official WNBA headshot using WNBA-compatible request headers and serves it to the
browser locally. If the official CDN has no headshot for a player, the UI still
falls back to the player's initials.


## v6 Render-safe fallback

Cloud hosts such as Render can occasionally receive an HTML challenge/block page
from `stats.wnba.com`, even when the same WNBA Stats request works from a home PC.

v6 now:

1. Tries official WNBA Stats first.
2. If the WNBA Stats player request fails, automatically loads WNBA teams and
   rosters from ESPN.
3. In fallback mode, player analysis uses ESPN's WNBA athlete game-log data and
   normalizes it into the same PTS / REB / AST / FG3M format.
4. Applies the exact same August/September L10, L5, H2H and strict pass rules.
5. Clearly labels fallback mode in the page rather than silently changing sources.
6. Safely handles non-JSON server responses, so an upstream HTML error page no
   longer produces the confusing "Unexpected token '<'" browser error.
7. Includes `render.yaml` and Gunicorn for easier Render deployment.

The official WNBA Stats source remains the primary source whenever the host can
reach it.
