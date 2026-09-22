
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any

import requests
from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)

WNBA_STATS_BASE = "https://stats.wnba.com/stats"
LEAGUE_ID = "10"

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


def stats_get(endpoint: str, params: dict[str, Any], timeout: int = 20) -> dict:
    """GET JSON from stats.wnba.com with a small retry loop."""
    url = f"{WNBA_STATS_BASE}/{endpoint}"
    last_error = None

    for attempt in range(3):
        try:
            response = SESSION.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))

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


@app.route("/")
def home():
    return render_template("index.html")


@app.get("/api/players")
def api_players():
    season = request.args.get("season", "2026")
    try:
        return jsonify({"players": league_players(season), "source": "stats.wnba.com"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


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
            # Do not fail the entire prop analysis if the Leaders endpoint is
            # temporarily throttled; expose the cross-check status to the UI.
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
