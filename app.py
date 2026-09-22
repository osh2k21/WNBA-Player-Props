
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
ESPN_CORE_V2_BASE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/wnba"
ESPN_CORE_V3_BASE = "https://sports.core.api.espn.com/v3/sports/basketball/wnba"

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



def espn_core_get(url: str, params: dict[str, Any] | None = None, timeout: int = 10) -> dict:
    """
    Fetch JSON from ESPN's sports.core.api.espn.com host.

    This host is used for the Render fallback because site.api.espn.com can
    return HTTP 403 from some cloud-provider IP ranges.
    """
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": HEADERS["User-Agent"],
        "Referer": "https://www.espn.com/",
        "Origin": "https://www.espn.com",
    }

    last_error = None
    for attempt in range(2):
        try:
            response = requests.get(
                url,
                params=params or {},
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
            )
            response.raise_for_status()
            text = response.text.lstrip()
            if text.startswith("<"):
                raise RuntimeError(
                    f"ESPN Core returned HTML instead of JSON (HTTP {response.status_code})."
                )
            return response.json()
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.4)

    raise RuntimeError(f"ESPN Core request failed: {last_error}")


def _first_nonempty(mapping: dict, *keys):
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _extract_core_items(payload: dict) -> list[dict]:
    """Accept the common ESPN Core v2/v3 collection shapes."""
    for key in ("items", "athletes", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

    # Some v3 responses wrap collections one level deeper.
    for key in ("data", "content"):
        value = payload.get(key)
        if isinstance(value, dict):
            nested = _extract_core_items(value)
            if nested:
                return nested

    return []


def _extract_team_from_core_athlete(athlete: dict) -> tuple[str, str, str | None]:
    """
    Return (team_id, abbreviation, logo_url) from several ESPN athlete schemas.
    """
    candidate = athlete.get("team")

    if not isinstance(candidate, dict):
        teams = athlete.get("teams")
        if isinstance(teams, list) and teams:
            first = teams[0]
            if isinstance(first, dict):
                candidate = first

    if not isinstance(candidate, dict):
        candidate = {}

    team_id = str(
        _first_nonempty(
            candidate,
            "id",
            "teamId",
            "uid",
        )
        or _first_nonempty(athlete, "teamId", "team_id")
        or ""
    )

    abbr = str(
        _first_nonempty(
            candidate,
            "abbreviation",
            "abbr",
            "shortDisplayName",
        )
        or _first_nonempty(athlete, "teamAbbreviation", "teamAbbr")
        or ""
    ).upper()

    logo_url = None
    logos = candidate.get("logos")
    if isinstance(logos, list) and logos:
        first_logo = logos[0]
        if isinstance(first_logo, dict):
            logo_url = first_logo.get("href")
    elif isinstance(candidate.get("logo"), str):
        logo_url = candidate.get("logo")

    return team_id, abbr, logo_url


def _extract_headshot(athlete: dict) -> str | None:
    headshot = athlete.get("headshot")
    if isinstance(headshot, dict):
        return headshot.get("href")
    if isinstance(headshot, str):
        return headshot

    images = athlete.get("images")
    if isinstance(images, list):
        for img in images:
            if isinstance(img, dict) and img.get("href"):
                return img["href"]

    return None


def _extract_team_ref(athlete: dict) -> str | None:
    """
    ESPN Core often returns athlete.team as {"$ref": ".../teams/{id}"} rather
    than embedding the team object. Return that reference so we can resolve the
    small set of unique WNBA teams once and reuse them for every athlete.
    """
    candidate = athlete.get("team")
    if isinstance(candidate, dict):
        ref = candidate.get("$ref")
        if isinstance(ref, str) and ref.startswith("http"):
            return ref

    teams = athlete.get("teams")
    if isinstance(teams, list):
        for item in teams:
            if isinstance(item, dict):
                ref = item.get("$ref")
                if isinstance(ref, str) and ref.startswith("http"):
                    return ref

    return None


def _normalize_core_team(team: dict) -> dict:
    """
    Normalize an ESPN Core team document into the fields used by the UI.
    """
    team_id = str(_first_nonempty(team, "id", "teamId") or "")
    abbr = str(
        _first_nonempty(
            team,
            "abbreviation",
            "abbr",
            "shortDisplayName",
        )
        or ""
    ).upper()

    logo_url = None
    logos = team.get("logos")
    if isinstance(logos, list) and logos:
        first_logo = logos[0]
        if isinstance(first_logo, dict):
            logo_url = first_logo.get("href")
    elif isinstance(team.get("logo"), str):
        logo_url = team.get("logo")

    return {
        "team_id": team_id,
        "team": abbr,
        "team_logo": logo_url,
    }


def _normalize_core_athlete(athlete: dict) -> dict | None:
    athlete_id = str(_first_nonempty(athlete, "id", "athleteId") or "")
    if not athlete_id:
        return None

    name = str(
        _first_nonempty(
            athlete,
            "fullName",
            "displayName",
            "name",
            "shortName",
        )
        or ""
    )

    team_id, team_abbr, team_logo = _extract_team_from_core_athlete(athlete)

    if not name:
        return None

    return {
        "player_id": f"espn:{athlete_id}",
        "provider_player_id": athlete_id,
        "provider": "espn-core",
        "player_name": name,
        "team_id": team_id,
        "team": team_abbr,
        "team_logo": team_logo,
        "photo_url": _extract_headshot(athlete),
        "season_pts": None,
        "season_reb": None,
        "season_ast": None,
        "season_fg3m": None,
        "season_min": None,
        "gp": None,
    }


def espn_core_players(season: str) -> list[dict]:
    """
    Load active WNBA athletes from ESPN Core API.

    ESPN Core athlete collections frequently provide the current team as a
    `$ref` URL instead of embedding abbreviation/team metadata. We resolve the
    unique team references (normally only ~15-18 WNBA teams), cache them, and
    attach that team metadata to every athlete.
    """
    cache_key = f"espn-core-players:{season}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    errors = []
    payload = None

    core_attempts = [
        (
            f"{ESPN_CORE_V3_BASE}/athletes",
            {"active": "true", "limit": 1000},
        ),
        (
            f"{ESPN_CORE_V2_BASE}/athletes",
            {"active": "true", "limit": 1000},
        ),
    ]

    for url, params in core_attempts:
        try:
            candidate = espn_core_get(url, params=params, timeout=10)
            items = _extract_core_items(candidate)
            if items:
                payload = candidate
                break
            errors.append(f"{url}: no athlete items")
        except Exception as exc:
            errors.append(f"{url}: {exc}")

    if payload is None:
        raise RuntimeError(
            "ESPN Core athlete directory failed. " + " | ".join(errors[:2])
        )

    raw_athletes = _extract_core_items(payload)

    # First pass: normalize what is embedded and collect unresolved team refs.
    pending = []
    team_refs = set()
    ready = []

    for athlete in raw_athletes:
        normalized = _normalize_core_athlete(athlete)
        if not normalized:
            continue

        if normalized.get("team"):
            ready.append(normalized)
            continue

        team_ref = _extract_team_ref(athlete)
        pending.append((normalized, athlete, team_ref))
        if team_ref:
            team_refs.add(team_ref)

    # Resolve each unique team ref only once.
    team_cache = {}

    def fetch_team_ref(ref: str):
        try:
            payload = espn_core_get(ref, timeout=7)
            return ref, _normalize_core_team(payload), None
        except Exception as exc:
            return ref, None, str(exc)

    if team_refs:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(fetch_team_ref, ref) for ref in team_refs]
            for future in as_completed(futures):
                ref, team_data, error = future.result()
                if team_data and team_data.get("team"):
                    team_cache[ref] = team_data
                elif error:
                    errors.append(f"team-ref {ref}: {error}")

    # Attach resolved team metadata.
    still_missing = []
    for normalized, raw, team_ref in pending:
        team_data = team_cache.get(team_ref) if team_ref else None
        if team_data:
            normalized["team_id"] = team_data.get("team_id", "")
            normalized["team"] = team_data.get("team", "")
            normalized["team_logo"] = team_data.get("team_logo")
            ready.append(normalized)
        else:
            still_missing.append((normalized, raw))

    # Last resort: fetch individual athlete profiles. Those profiles usually
    # carry a resolvable team $ref even when the collection response is sparse.
    if still_missing:
        def fetch_profile(entry):
            normalized, _raw = entry
            athlete_id = normalized["provider_player_id"]
            for base in (ESPN_CORE_V3_BASE, ESPN_CORE_V2_BASE):
                try:
                    profile = espn_core_get(
                        f"{base}/athletes/{athlete_id}",
                        timeout=7,
                    )

                    upgraded = _normalize_core_athlete(profile) or normalized
                    if upgraded.get("team"):
                        return upgraded

                    ref = _extract_team_ref(profile)
                    if ref:
                        if ref in team_cache:
                            team_data = team_cache[ref]
                        else:
                            team_doc = espn_core_get(ref, timeout=7)
                            team_data = _normalize_core_team(team_doc)
                            if team_data.get("team"):
                                team_cache[ref] = team_data

                        if team_data and team_data.get("team"):
                            upgraded["team_id"] = team_data.get("team_id", "")
                            upgraded["team"] = team_data.get("team", "")
                            upgraded["team_logo"] = team_data.get("team_logo")
                            return upgraded
                except Exception:
                    continue
            return normalized

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(fetch_profile, entry) for entry in still_missing]
            for future in as_completed(futures):
                player = future.result()
                if player.get("team"):
                    ready.append(player)

    # Deduplicate by athlete/team and ignore free agents / unresolved entries.
    deduped = {}
    for p in ready:
        if not p.get("team"):
            continue
        key = (p["provider_player_id"], p["team"])
        deduped[key] = p

    players = list(deduped.values())
    players.sort(key=lambda x: (x["team"], x["player_name"]))

    if not players:
        diagnostic_refs = len(team_refs)
        raise RuntimeError(
            "ESPN Core returned athletes, but team references could not be resolved "
            f"into current WNBA teams. Found {len(raw_athletes)} athletes and "
            f"{diagnostic_refs} unique team references."
        )

    _cache_set(cache_key, players)
    return players


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
    Source order:
      Local/non-cloud: Official WNBA Stats -> ESPN Core
      Render/cloud: ESPN Core directly

    stats.wnba.com and site.api.espn.com can both reject datacenter IPs, so the
    online roster path uses sports.core.api.espn.com instead.
    """
    on_render = bool(os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID"))

    if on_render:
        core_players = espn_core_players(season)
        return (
            core_players,
            "ESPN Core fallback",
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
        core_players = espn_core_players(season)
        return (
            core_players,
            "ESPN Core fallback",
            f"Official WNBA Stats was unavailable, so the app switched to ESPN Core. {exc}",
        )


def _made_from_attempt_string(value: Any) -> float:
    text = str(value or "").strip()
    if "-" in text:
        text = text.split("-", 1)[0]
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0



def _https_ref(value: Any) -> str | None:
    """Normalize ESPN Core $ref URLs to HTTPS."""
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("http://"):
        return "https://" + value[len("http://"):]
    if value.startswith("https://"):
        return value
    return None


def _core_stat_map(payload: dict) -> dict[str, float]:
    """
    Flatten ESPN Core player-event statistics into a semantic stat map.
    Basketball feeds can expose the same stat under slightly different names,
    so collect name, abbreviation and displayName aliases.
    """
    out: dict[str, float] = {}

    def put(key: Any, raw_value: Any):
        if not key:
            return
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            # Some display values may be "3-8"; made baskets are handled below.
            text = str(raw_value or "").strip()
            if "-" in text:
                try:
                    value = float(text.split("-", 1)[0])
                except ValueError:
                    return
            else:
                return
        out[str(key).strip().upper()] = value

    splits = payload.get("splits")
    if isinstance(splits, dict):
        categories = splits.get("categories") or []
    else:
        categories = payload.get("categories") or []

    if isinstance(categories, dict):
        categories = list(categories.values())

    for category in categories if isinstance(categories, list) else []:
        if not isinstance(category, dict):
            continue
        stats = category.get("stats") or []
        if isinstance(stats, dict):
            stats = list(stats.values())

        for stat in stats if isinstance(stats, list) else []:
            if not isinstance(stat, dict):
                continue
            raw = stat.get("value")
            if raw is None:
                raw = stat.get("displayValue")

            for key in (
                stat.get("name"),
                stat.get("abbreviation"),
                stat.get("displayName"),
                stat.get("shortDisplayName"),
            ):
                put(key, raw)

    return out


def _stat_pick(stat_map: dict[str, float], *aliases: str, default: float = 0.0) -> float:
    for alias in aliases:
        key = alias.upper()
        if key in stat_map:
            return stat_map[key]
    return default


def _event_short_name(event: dict) -> str:
    return str(
        event.get("shortName")
        or event.get("name")
        or event.get("displayName")
        or ""
    )


def _opponent_from_short_name(short_name: str, team_abbr: str) -> str:
    """
    Convert ESPN event names such as 'MIN @ IND' / 'MIN vs IND' into opponent
    abbreviation when possible.
    """
    text = (short_name or "").upper().replace("VS.", "VS").replace(" AT ", " @ ")
    team = team_abbr.upper().strip()

    for token in (" @ ", " VS "):
        if token in text:
            left, right = [x.strip() for x in text.split(token, 1)]
            if left == team:
                return right
            if right == team:
                return left

    # Fall back to any short uppercase token that is not the player's team.
    tokens = re.findall(r"\b[A-Z]{2,4}\b", text)
    for token in tokens:
        if token != team:
            return token
    return ""


def espn_core_player_logs(
    espn_player_id: str,
    season: str,
    team_abbr: str,
) -> list[dict]:
    """
    Build the WNBA game log entirely from ESPN Core.

    ESPN's Web gamelog endpoint does not consistently expose WNBA events.
    Core's season athlete eventlog is more stable and includes per-game refs:
      /seasons/{year}/athletes/{id}/eventlog

    Each eventlog item points to the event document and that player's game
    statistics document. We resolve those refs concurrently and normalize them
    into GAME_DATE / MATCHUP / MIN / PTS / REB / AST / FG3M.
    """
    cache_key = f"espn-core-gamelog:{season}:{espn_player_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    eventlog_url = (
        f"{ESPN_CORE_V2_BASE}/seasons/{season}/athletes/"
        f"{espn_player_id}/eventlog"
    )
    eventlog = espn_core_get(eventlog_url, {"limit": 100}, timeout=10)

    events_block = eventlog.get("events") or {}
    if isinstance(events_block, dict):
        items = events_block.get("items") or []
    elif isinstance(events_block, list):
        items = events_block
    else:
        items = []

    if not items:
        raise RuntimeError(
            "ESPN Core eventlog did not contain any games for this player/season."
        )

    jobs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("played") is False:
            continue

        event_ref = None
        stats_ref = None

        event_obj = item.get("event")
        if isinstance(event_obj, dict):
            event_ref = _https_ref(event_obj.get("$ref"))

        stats_obj = item.get("statistics")
        if isinstance(stats_obj, dict):
            stats_ref = _https_ref(stats_obj.get("$ref"))

        if event_ref and stats_ref:
            jobs.append(
                {
                    "event_ref": event_ref,
                    "stats_ref": stats_ref,
                    "team_id": str(item.get("teamId", "")),
                }
            )

    if not jobs:
        raise RuntimeError(
            "ESPN Core eventlog returned games, but no usable event/statistics references."
        )

    # Fetch all unique refs concurrently. A full WNBA season is small enough
    # that this remains quick while avoiding the unsupported Web gamelog.
    refs = set()
    for job in jobs:
        refs.add(job["event_ref"])
        refs.add(job["stats_ref"])

    docs: dict[str, dict] = {}
    errors = []

    def fetch_ref(ref: str):
        try:
            return ref, espn_core_get(ref, timeout=8), None
        except Exception as exc:
            return ref, None, str(exc)

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(fetch_ref, ref) for ref in refs]
        for future in as_completed(futures):
            ref, payload, error = future.result()
            if payload is not None:
                docs[ref] = payload
            elif error:
                errors.append(error)

    rows = []
    for job in jobs:
        event = docs.get(job["event_ref"])
        stats_payload = docs.get(job["stats_ref"])
        if not isinstance(event, dict) or not isinstance(stats_payload, dict):
            continue

        date = event.get("date")
        short_name = _event_short_name(event)
        opponent = _opponent_from_short_name(short_name, team_abbr)

        # Keep MATCHUP compatible with the existing H2H matcher.
        matchup = short_name
        if opponent and team_abbr.upper() not in matchup.upper():
            matchup = f"{team_abbr.upper()} vs. {opponent}"

        stat_map = _core_stat_map(stats_payload)

        row = {
            "GAME_DATE": date,
            "MATCHUP": matchup,
            "MIN": _stat_pick(
                stat_map,
                "MIN",
                "MINUTES",
                "MINUTESPLAYED",
            ),
            "PTS": _stat_pick(
                stat_map,
                "PTS",
                "POINTS",
            ),
            "REB": _stat_pick(
                stat_map,
                "REB",
                "REBOUNDS",
                "TOTALREBOUNDS",
            ),
            "AST": _stat_pick(
                stat_map,
                "AST",
                "ASSISTS",
            ),
            "FG3M": _stat_pick(
                stat_map,
                "3PM",
                "FG3M",
                "THREEPOINTFIELDGOALSMADE",
                "THREEPOINTSMADE",
            ),
        }
        rows.append(row)

    rows.sort(
        key=lambda r: parse_game_date(r.get("GAME_DATE")) or datetime.min,
        reverse=True,
    )

    if not rows:
        detail = errors[0] if errors else "No resolved event/stat rows."
        raise RuntimeError(f"ESPN Core could not build the player game log. {detail}")

    _cache_set(cache_key, rows)
    return rows


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
                    f"WNBA Stats service or ESPN Core fallback. {exc}"
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
            players = espn_core_players(season)
            player = next((p for p in players if p["player_id"] == player_id), None)
            if not player:
                raise RuntimeError("Selected ESPN Core player could not be found.")

            provider_id = player_id.split(":", 1)[1]
            logs = espn_core_player_logs(
                provider_id,
                season,
                player.get("team", ""),
            )
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
                    "source": "ESPN Core roster + ESPN Core event logs (online fallback mode)",
                    "note": (
                        "The hosting server could not reliably reach stats.wnba.com, so this "
                        "analysis used ESPN Core for both the WNBA roster and player event/game statistics. "
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
