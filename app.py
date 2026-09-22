
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

import requests
from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)

WNBA_STATS_BASE = "https://stats.wnba.com/stats"
LEAGUE_ID = "10"

ESPN_SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"
ESPN_WEB_BASE = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/wnba"

# Browser-like headers are important for the official WNBA/NBA stats service.
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Origin": "https://stats.wnba.com",
    "Referer": "https://stats.wnba.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/153.0.0.0 Safari/537.36"
    ),
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

CACHE: dict[str, tuple[float, Any]] = {}
CACHE_TTL_SECONDS = 300


def _cache_get(key: str):
    item = CACHE.get(key)
    if not item:
        return None
    created, value = item
    if time.time() - created > CACHE_TTL_SECONDS:
        CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any):
    CACHE[key] = (time.time(), value)


def parse_game_date(value):
    """Parse the date formats returned by the official WNBA Stats feeds."""
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    # Common API forms include:
    # 2026-09-20T00:00:00
    # 2026-09-20
    # 09/20/2026
    # Sep 20, 2026
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%b %d %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    return None


def stats_get(endpoint: str, params: dict[str, Any], timeout: int = 8) -> dict:
    """GET JSON from stats.wnba.com with a small retry loop."""
    url = f"{WNBA_STATS_BASE}/{endpoint}"
    last_error = None

    for attempt in range(2):
        try:
            response = SESSION.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            if attempt < 1:
                time.sleep(0.5)

    raise RuntimeError(f"Official WNBA Stats request failed: {last_error}")


def rows_from_resultset(payload: dict, preferred_name: str | None = None) -> list[dict]:
    """
    Convert stats.wnba.com resultSets format to dictionaries.
    Supports both `resultSets: []` and `resultSet: {}` response shapes.
    """
    result_sets = payload.get("resultSets")

    if isinstance(result_sets, list):
        selected = None
        if preferred_name:
            for rs in result_sets:
                if rs.get("name") == preferred_name:
                    selected = rs
                    break
        selected = selected or (result_sets[0] if result_sets else None)
    else:
        selected = payload.get("resultSet")

    if not selected:
        return []

    headers = selected.get("headers", [])
    rows = selected.get("rowSet", [])
    return [dict(zip(headers, row)) for row in rows]


def league_players(season: str) -> list[dict]:
    cache_key = f"players:{season}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # This is the official stats endpoint behind the WNBA player stats tables.
    params = {
        "College": "",
        "Conference": "",
        "Country": "",
        "DateFrom": "",
        "DateTo": "",
        "Division": "",
        "DraftPick": "",
        "DraftYear": "",
        "GameScope": "",
        "GameSegment": "",
        "Height": "",
        "LastNGames": "0",
        "LeagueID": LEAGUE_ID,
        "Location": "",
        "MeasureType": "Base",
        "Month": "0",
        "OpponentTeamID": "0",
        "Outcome": "",
        "PORound": "0",
        "PaceAdjust": "N",
        "PerMode": "PerGame",
        "Period": "0",
        "PlayerExperience": "",
        "PlayerPosition": "",
        "PlusMinus": "N",
        "Rank": "N",
        "Season": season,
        "SeasonSegment": "",
        "SeasonType": "Regular Season",
        "ShotClockRange": "",
        "StarterBench": "",
        "TeamID": "0",
        "TwoWay": "0",
        "VsConference": "",
        "VsDivision": "",
        "Weight": "",
    }

    payload = stats_get("leaguedashplayerstats", params)
    rows = rows_from_resultset(payload, "LeagueDashPlayerStats")

    # Keep one entry per player/team pair. A traded player can appear for a current team
    # depending on the upstream response. The UI groups by TEAM_ABBREVIATION.
    players = []
    seen = set()
    for r in rows:
        player_id = str(r.get("PLAYER_ID", ""))
        team_id = str(r.get("TEAM_ID", ""))
        key = (player_id, team_id)
        if not player_id or key in seen:
            continue
        seen.add(key)
        players.append(
            {
                "player_id": player_id,
                "player_name": r.get("PLAYER_NAME", ""),
                "team_id": team_id,
                "team": r.get("TEAM_ABBREVIATION", ""),
                "season_pts": r.get("PTS"),
                "season_reb": r.get("REB"),
                "season_ast": r.get("AST"),
                "season_fg3m": r.get("FG3M"),
                "season_min": r.get("MIN"),
                "gp": r.get("GP"),
            }
        )

    players.sort(key=lambda x: (x["team"], x["player_name"]))
    _cache_set(cache_key, players)
    return players



def league_leaders(season: str, stat_category: str) -> list[dict]:
    """
    Pull official WNBA season leaders. This is the stats service used by
    https://stats.wnba.com/leaders/ and provides a second official cross-check
    for season-level PTS/REB/AST/FG3M data.
    """
    supported = {"PTS", "REB", "AST", "FG3M"}
    if stat_category not in supported:
        raise ValueError("Unsupported leader category.")

    cache_key = f"leaders:{season}:{stat_category}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    params = {
        "LeagueID": LEAGUE_ID,
        "PerMode": "PerGame",
        "Scope": "S",
        "Season": season,
        "SeasonType": "Regular Season",
        "StatCategory": stat_category,
    }

    payload = stats_get("leagueleaders", params)
    rows = rows_from_resultset(payload, "LeagueLeaders")
    if not rows:
        rows = rows_from_resultset(payload)

    _cache_set(cache_key, rows)
    return rows


def player_leader_context(player_id: str, season: str, stat_category: str) -> dict:
    rows = league_leaders(season, stat_category)
    for idx, row in enumerate(rows, start=1):
        if str(row.get("PLAYER_ID", "")) == str(player_id):
            # The leader feed commonly includes RANK and the stat itself.
            rank = row.get("RANK") or idx
            value = row.get(stat_category)
            if value is None:
                # Some feeds expose the requested value through PTS even for
                # category-specific responses; keep a safe fallback.
                value = row.get("PTS")
            return {
                "rank": rank,
                "value": value,
                "player_name": row.get("PLAYER"),
                "team": row.get("TEAM"),
            }
    return {"rank": None, "value": None, "player_name": None, "team": None}



def player_logs(player_id: str, season: str) -> list[dict]:
    cache_key = f"logs:{season}:{player_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    params = {
        "DateFrom": "",
        "DateTo": "",
        "GameSegment": "",
        "LastNGames": "0",
        "LeagueID": LEAGUE_ID,
        "Location": "",
        "MeasureType": "Base",
        "Month": "0",
        "OpposingTeamID": "0",
        "Outcome": "",
        "PORound": "0",
        "PerMode": "Totals",
        "Period": "0",
        "PlayerID": player_id,
        "Season": season,
        "SeasonSegment": "",
        "SeasonType": "Regular Season",
        "ShotClockRange": "",
        "TeamID": "0",
        "VsConference": "",
        "VsDivision": "",
    }

    payload = stats_get("playergamelogs", params)
    rows = rows_from_resultset(payload, "PlayerGameLogs")

    rows.sort(
        key=lambda r: parse_game_date(r.get("GAME_DATE")) or datetime.min,
        reverse=True,
    )
    _cache_set(cache_key, rows)
    return rows



def espn_get(url: str, params: dict[str, Any] | None = None, timeout: int = 8) -> dict:
    """Fetch JSON from ESPN's public web data endpoints with short cloud-safe timeouts."""
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": HEADERS["User-Agent"],
        "Referer": "https://www.espn.com/",
    }
    last_error = None
    for attempt in range(2):
        try:
            response = requests.get(
                url,
                params=params or {},
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            text = response.text.lstrip()
            if text.startswith("<"):
                raise RuntimeError(
                    f"ESPN returned HTML instead of JSON (HTTP {response.status_code})."
                )
            return response.json()
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.35)

    raise RuntimeError(f"ESPN fallback request failed: {last_error}")


def _espn_team_entries(payload: dict) -> list[dict]:
    """Extract team objects from ESPN's WNBA teams response."""
    teams = []
    try:
        sports = payload.get("sports", [])
        for sport in sports:
            for league in sport.get("leagues", []):
                for wrapper in league.get("teams", []):
                    team = wrapper.get("team", wrapper)
                    if isinstance(team, dict) and team.get("id"):
                        teams.append(team)
    except Exception:
        pass
    return teams


def _flatten_roster_athletes(payload: dict) -> list[dict]:
    """Handle both flat and grouped ESPN roster shapes."""
    out = []
    raw = payload.get("athletes", [])
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            if item.get("id") and (item.get("fullName") or item.get("displayName")):
                out.append(item)
                continue
            for key in ("items", "athletes"):
                nested = item.get(key)
                if isinstance(nested, list):
                    for athlete in nested:
                        if isinstance(athlete, dict) and athlete.get("id"):
                            out.append(athlete)
    return out


def espn_league_players(season: str) -> list[dict]:
    """
    Render-safe player-list fallback.

    ESPN's team endpoint supplies the WNBA team list. Team rosters are fetched
    concurrently so a cold Render instance does not spend 30+ seconds loading
    rosters one at a time.
    """
    cache_key = f"espn-players:{season}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    teams_payload = espn_get(f"{ESPN_SITE_BASE}/teams", {"limit": 100}, timeout=8)
    team_entries = _espn_team_entries(teams_payload)
    if not team_entries:
        raise RuntimeError("ESPN returned no WNBA teams.")

    normalized_teams = []
    for team in team_entries:
        team_id = str(team.get("id", ""))
        abbr = str(team.get("abbreviation", "")).upper()
        if not team_id or not abbr:
            continue

        logos = team.get("logos") or []
        logo_url = None
        if isinstance(logos, list) and logos:
            logo_url = logos[0].get("href")

        normalized_teams.append(
            {
                "team_id": team_id,
                "abbr": abbr,
                "logo_url": logo_url,
            }
        )

    if not normalized_teams:
        raise RuntimeError("ESPN team list was present but contained no usable teams.")

    def fetch_roster(team_info: dict) -> tuple[dict, dict | None, str | None]:
        url = f"{ESPN_SITE_BASE}/teams/{team_info['team_id']}/roster"
        try:
            return team_info, espn_get(url, timeout=7), None
        except Exception as exc:
            return team_info, None, str(exc)

    roster_results = []
    errors = []

    # A small pool is faster than sequential requests without hammering ESPN.
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(fetch_roster, t) for t in normalized_teams]
        for future in as_completed(futures):
            team_info, roster, error = future.result()
            if roster is not None:
                roster_results.append((team_info, roster))
            elif error:
                errors.append(f"{team_info['abbr']}: {error}")

    players = []
    seen = set()

    for team_info, roster in roster_results:
        for athlete in _flatten_roster_athletes(roster):
            espn_id = str(athlete.get("id", ""))
            if not espn_id:
                continue

            key = (espn_id, team_info["team_id"])
            if key in seen:
                continue
            seen.add(key)

            headshot = athlete.get("headshot")
            photo_url = headshot.get("href") if isinstance(headshot, dict) else None

            players.append(
                {
                    "player_id": f"espn:{espn_id}",
                    "provider_player_id": espn_id,
                    "provider": "espn",
                    "player_name": athlete.get("fullName")
                    or athlete.get("displayName")
                    or athlete.get("shortName")
                    or "",
                    "team_id": team_info["team_id"],
                    "team": team_info["abbr"],
                    "team_logo": team_info["logo_url"],
                    "photo_url": photo_url,
                    "season_pts": None,
                    "season_reb": None,
                    "season_ast": None,
                    "season_fg3m": None,
                    "season_min": None,
                    "gp": None,
                }
            )

    if not players:
        detail = "; ".join(errors[:4])
        raise RuntimeError(
            "ESPN fallback returned no WNBA roster players."
            + (f" Roster errors: {detail}" if detail else "")
        )

    players.sort(key=lambda x: (x["team"], x["player_name"]))
    _cache_set(cache_key, players)
    return players


def players_with_fallback(season: str) -> tuple[list[dict], str, str | None]:
    """
    Prefer official WNBA Stats on normal/local networks.

    Render cloud IPs are frequently challenged by stats.wnba.com. On Render we
    go directly to the ESPN fallback instead of waiting for the official request
    to time out and causing a Gunicorn 500/worker timeout.
    """
    on_render = bool(os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID"))

    if on_render:
        fallback = espn_league_players(season)
        return (
            fallback,
            "ESPN fallback",
            "Online fallback mode is active because stats.wnba.com can block Render cloud requests.",
        )

    try:
        official = league_players(season)
        if official:
            for p in official:
                p.setdefault("provider", "wnba")
                p.setdefault("provider_player_id", p.get("player_id"))
                p.setdefault("team_logo", None)
                p.setdefault("photo_url", None)
            return official, "Official WNBA Stats", None
        raise RuntimeError("Official WNBA Stats returned no players.")
    except Exception as exc:
        fallback = espn_league_players(season)
        return (
            fallback,
            "ESPN fallback",
            f"Official WNBA Stats was unavailable, so the app switched to ESPN. {exc}",
        )


def _made_from_attempt_string(value: Any) -> float:
    text = str(value or "").strip()
    if "-" in text:
        text = text.split("-", 1)[0]
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def espn_player_logs(espn_player_id: str, season: str, team_abbr: str) -> list[dict]:
    """
    Normalize ESPN's athlete gamelog into the same fields used by the WNBA
    Stats analyzer: GAME_DATE, MATCHUP, MIN, PTS, REB, AST, FG3M.
    """
    cache_key = f"espn-gamelog:{season}:{espn_player_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    payload = espn_get(
        f"{ESPN_WEB_BASE}/athletes/{espn_player_id}/gamelog",
        {"season": season},
    )

    labels = payload.get("labels") or []
    names = payload.get("names") or []
    events = payload.get("events") or []

    if not isinstance(events, list):
        raise RuntimeError("ESPN gamelog did not contain an events list.")

    # Stats usually map to labels after DATE / OPP / RESULT. Use semantic names
    # when possible and label fallbacks otherwise.
    meta_count = max(0, len(labels) - (len(events[0].get("stats", [])) if events else 0))
    stat_labels = labels[meta_count:] if labels else []
    stat_names = names[meta_count:] if names else []

    rows = []
    for event in events:
        if not isinstance(event, dict):
            continue

        stats = event.get("stats") or []
        stat_map = {}
        for i, raw in enumerate(stats):
            if i < len(stat_labels):
                stat_map[str(stat_labels[i]).upper()] = raw
            if i < len(stat_names):
                stat_map[str(stat_names[i])] = raw

        opponent = event.get("opponent") or {}
        opp_abbr = str(opponent.get("abbreviation") or "").upper()

        # ESPN sometimes provides atVs/homeAway. H2H only requires opponent
        # abbreviation to be present in MATCHUP, so use a stable representation.
        matchup = f"{team_abbr.upper()} vs. {opp_abbr}" if opp_abbr else team_abbr.upper()

        def pick(*keys, default=0):
            for key in keys:
                if key in stat_map and stat_map[key] not in (None, ""):
                    return stat_map[key]
            return default

        row = {
            "GAME_DATE": event.get("date"),
            "MATCHUP": matchup,
            "MIN": pick("MIN", "minutes", default=0),
            "PTS": pick("PTS", "points", default=0),
            "REB": pick("REB", "rebounds", default=0),
            "AST": pick("AST", "assists", default=0),
            "FG3M": _made_from_attempt_string(
                pick("3PT", "3PM", "threePointsMade", "threePointFieldGoalsMade", default=0)
            ),
        }
        rows.append(row)

    rows.sort(
        key=lambda r: parse_game_date(r.get("GAME_DATE")) or datetime.min,
        reverse=True,
    )

    if not rows:
        raise RuntimeError("ESPN returned no game-log rows for this player.")

    _cache_set(cache_key, rows)
    return rows


def averages_from_logs(logs: list[dict], stat_key: str) -> tuple[float | None, float | None]:
    """Compute season stat average and minutes from the normalized game log."""
    if not logs:
        return None, None

    values = []
    minutes = []
    for g in logs:
        try:
            values.append(float(g.get(stat_key, 0)))
        except (TypeError, ValueError):
            pass
        try:
            minutes.append(float(g.get("MIN", 0)))
        except (TypeError, ValueError):
            pass

    avg = round(sum(values) / len(values), 1) if values else None
    min_avg = round(sum(minutes) / len(minutes), 1) if minutes else None
    return avg, min_avg


def threshold_for_sample(n: int) -> int | None:
    # User's strict rule:
    # 10 games -> 9/10
    # 9 games -> 8/9
    # 8 games -> 7/8
    # Under 8 games -> not enough data for HOT.
    if n >= 10:
        return 9
    if n == 9:
        return 8
    if n == 8:
        return 7
    return None


def analyze_prop(
    logs: list[dict],
    stat_key: str,
    line: float,
    opponent_abbr: str,
    august_september_only: bool = True,
):
    valid_stats = {"PTS", "REB", "AST", "FG3M"}
    if stat_key not in valid_stats:
        raise ValueError("Unsupported stat type.")

    # Exact recent sample: August + September only, newest first.
    recent_pool = []
    skipped_unparsed_dates = []
    for g in logs:
        parsed = parse_game_date(g.get("GAME_DATE"))
        if not parsed:
            skipped_unparsed_dates.append(str(g.get("GAME_DATE", "")))
            continue

        if august_september_only and parsed.month not in (8, 9):
            continue

        recent_pool.append(g)

    l10_games = recent_pool[:10]
    l5_games = recent_pool[:5]

    def value(game):
        raw = game.get(stat_key)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    def display_number(v):
        return int(v) if float(v).is_integer() else round(v, 1)

    l10_vals = [display_number(value(g)) for g in l10_games]
    l5_vals = [display_number(value(g)) for g in l5_games]
    l10_hits = sum(value(g) >= line for g in l10_games)
    l5_hits = sum(value(g) >= line for g in l5_games)

    # H2H is current-season regular season, not limited to Aug/Sep.
    # MATCHUP examples: "IND vs. MIN", "IND @ MIN".
    h2h_games = []
    opp = opponent_abbr.upper().strip()
    for g in logs:
        matchup = str(g.get("MATCHUP", "")).upper()
        if opp and opp in matchup:
            h2h_games.append(g)

    h2h_vals = [display_number(value(g)) for g in h2h_games]
    h2h_hits = sum(value(g) >= line for g in h2h_games)

    l10_req = threshold_for_sample(len(l10_games))
    l10_pass = l10_req is not None and l10_hits >= l10_req
    l5_pass = len(l5_games) >= 5 and l5_hits >= 4
    h2h_pass = len(h2h_games) > 0 and h2h_hits == len(h2h_games)

    if l10_pass and l5_pass and h2h_pass:
        verdict = "🔥 STATISTICAL PASS"
        verdict_code = "hot"
    elif l10_pass and l5_pass:
        verdict = "🟡 CLOSE — H2H DOES NOT FULLY PASS"
        verdict_code = "close"
    else:
        verdict = "❌ FAIL"
        verdict_code = "fail"

    return {
        "line": line,
        "stat": stat_key,
        "l10_values": l10_vals,
        "l10_hits": l10_hits,
        "l10_games": len(l10_games),
        "l10_required": l10_req,
        "l10_pass": l10_pass,
        "l5_values": l5_vals,
        "l5_hits": l5_hits,
        "l5_games": len(l5_games),
        "l5_pass": l5_pass,
        "h2h_values": h2h_vals,
        "h2h_hits": h2h_hits,
        "h2h_games": len(h2h_games),
        "h2h_pass": h2h_pass,
        "verdict": verdict,
        "verdict_code": verdict_code,
        "debug": {
            "total_games_received": len(logs),
            "aug_sep_games_found": len(recent_pool),
            "unparsed_game_dates": skipped_unparsed_dates[:5],
        },
        "recent_games": [
            {
                "date": g.get("GAME_DATE"),
                "matchup": g.get("MATCHUP"),
                "min": g.get("MIN"),
                "value": display_number(value(g)),
            }
            for g in l10_games
        ],
        "h2h_games_detail": [
            {
                "date": g.get("GAME_DATE"),
                "matchup": g.get("MATCHUP"),
                "min": g.get("MIN"),
                "value": display_number(value(g)),
            }
            for g in h2h_games
        ],
    }



@app.get("/api/player-image/<player_id>")
def api_player_image(player_id: str):
    """
    Proxy the official WNBA player headshot through the local Flask server.

    Direct hotlinking the headshot CDN from localhost can fail in some browsers,
    even though the same image is used on the official WNBA player page. The
    proxy lets the backend request it with WNBA-compatible headers and then
    returns the image to the local UI.
    """
    if not player_id.isdigit():
        return Response(status=404)

    cache_key = f"player-image:{player_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        body, content_type = cached
        return Response(
            body,
            status=200,
            content_type=content_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    candidates = [
        f"https://cdn.wnba.com/headshots/wnba/latest/1040x760/{player_id}.png",
        f"https://cdn.nba.com/headshots/wnba/latest/1040x760/{player_id}.png",
    ]

    image_headers = {
        **HEADERS,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": f"https://www.wnba.com/player/{player_id}",
    }

    for url in candidates:
        try:
            r = requests.get(url, headers=image_headers, timeout=15, allow_redirects=True)
            content_type = r.headers.get("content-type", "")
            if r.ok and content_type.startswith("image/") and r.content:
                _cache_set(cache_key, (r.content, content_type))
                return Response(
                    r.content,
                    status=200,
                    content_type=content_type,
                    headers={"Cache-Control": "public, max-age=86400"},
                )
        except Exception:
            continue

    # Returning 404 intentionally triggers the initials fallback in the browser.
    return Response(status=404)


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    # Keep API failures machine-readable. This prevents the browser from seeing
    # Render/Flask's HTML "Internal Server Error" page and producing JSON errors.
    if request.path.startswith("/api/"):
        app.logger.exception("API error: %s", exc)
        return jsonify(
            {
                "error": f"Server error: {type(exc).__name__}: {exc}",
            }
        ), 500

    raise exc


@app.route("/")
def home():
    return render_template("index.html")


@app.get("/api/players")
def api_players():
    season = request.args.get("season", "2026")
    try:
        players, source, warning = players_with_fallback(season)
        return jsonify(
            {
                "players": players,
                "source": source,
                "warning": warning,
            }
        )
    except Exception as exc:
        return jsonify(
            {
                "error": (
                    "Could not load the WNBA player list from either the official "
                    f"WNBA Stats service or the fallback source. {exc}"
                )
            }
        ), 502


@app.post("/api/analyze")
def api_analyze():
    data = request.get_json(force=True)
    season = str(data.get("season", "2026"))
    player_id = str(data.get("player_id", "")).strip()
    opponent = str(data.get("opponent", "")).strip().upper()
    stat = str(data.get("stat", "PTS")).strip().upper()

    try:
        line = float(data.get("line"))
    except (TypeError, ValueError):
        return jsonify({"error": "Line must be a number."}), 400

    if not player_id:
        return jsonify({"error": "Choose a player."}), 400
    if not opponent:
        return jsonify({"error": "Choose an opponent."}), 400

    try:
        # If the dropdown was loaded from ESPN fallback, the ID carries an
        # explicit prefix. Otherwise we use official WNBA Stats.
        if player_id.startswith("espn:"):
            players = espn_league_players(season)
            player = next((p for p in players if p["player_id"] == player_id), None)
            if not player:
                raise RuntimeError("Selected ESPN player could not be found.")

            provider_id = player_id.split(":", 1)[1]
            logs = espn_player_logs(provider_id, season, player.get("team", ""))
            analysis = analyze_prop(logs, stat, line, opponent)
            season_average, season_minutes = averages_from_logs(logs, stat)

            return jsonify(
                {
                    "player": player,
                    "season_average": season_average,
                    "season_minutes": season_minutes,
                    "leader_context": None,
                    "leader_error": "WNBA Leaders cross-check unavailable while using fallback mode.",
                    "analysis": analysis,
                    "source": "ESPN fallback (official WNBA Stats blocked from hosting server)",
                    "note": (
                        "The hosting server could not reach stats.wnba.com, so this "
                        "analysis used ESPN's WNBA roster and player game-log data. "
                        "The same strict August/September L10, L5 and current-season "
                        "H2H rules were applied. Injury/availability is still separate."
                    ),
                }
            )

        # Official WNBA path
        players = league_players(season)
        player = next((p for p in players if p["player_id"] == player_id), None)
        logs = player_logs(player_id, season)
        analysis = analyze_prop(logs, stat, line, opponent)

        stat_avg_field = {
            "PTS": "season_pts",
            "REB": "season_reb",
            "AST": "season_ast",
            "FG3M": "season_fg3m",
        }[stat]

        leader_context = None
        leader_error = None
        try:
            leader_context = player_leader_context(player_id, season, stat)
        except Exception as leader_exc:
            leader_error = str(leader_exc)

        return jsonify(
            {
                "player": player,
                "season_average": player.get(stat_avg_field) if player else None,
                "season_minutes": player.get("season_min") if player else None,
                "leader_context": leader_context,
                "leader_error": leader_error,
                "analysis": analysis,
                "source": (
                    "Official WNBA Stats: Players/Game Logs + Season Leaders "
                    "(stats.wnba.com)"
                ),
                "note": (
                    "This grade is statistical only. The official WNBA Stats feed "
                    "does not provide current injury/availability status, so injury "
                    "clearance should be checked separately before treating a pass as final."
                ),
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=True)
