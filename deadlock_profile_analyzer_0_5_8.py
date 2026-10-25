import json
import sys
import queue
import traceback
import logging
import shutil
import datetime
import os
import time
from pathlib import Path
from tkinter import filedialog
import statistics
import re
import threading
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import tkinter as tk
from tkinter import ttk, messagebox


API_BASE = "https://api.deadlock-api.com"
STEAM_BASE = "https://steamcommunity.com"
STEAM_ID_BASE = 76561197960265728
APP_VERSION = "0.5.8"
USER_AGENT = "DeadlockProfileAnalyzer/0.5.8"
REQUEST_TIMEOUT = 25


# ============================================================
# GENERAL HELPERS
# ============================================================

def request_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def request_text(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.read().decode("utf-8", errors="replace")


def api_get(path, params=None):
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    return request_json(url)


def as_list(data):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in (
            "data",
            "results",
            "players",
            "matches",
            "hero_stats",
            "heroStats",
        ):
            if isinstance(data.get(key), list):
                return data[key]

        return list(data.values())

    return []


def as_float(value, default=None):
    if value is None or isinstance(value, bool):
        return default

    try:
        if isinstance(value, str):
            value = value.strip().replace("%", "").replace(",", "")
            if not value:
                return default

        return float(value)

    except (ValueError, TypeError):
        return default


def as_int(value, default=None):
    number = as_float(value, default)
    return int(number) if number is not None else default


def first_value(data, names):
    if not isinstance(data, dict):
        return None

    for name in names:
        if name in data and data[name] is not None:
            return data[name]

    return None


def normalized_key(key):
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def get_field(record, field_names):
    if not isinstance(record, dict):
        return None

    wanted = {normalized_key(name) for name in field_names}

    for key, value in record.items():
        if normalized_key(key) in wanted and value is not None:
            return value

    return None


def walk_dicts(data, depth=0):
    if depth > 12:
        return

    if isinstance(data, dict):
        yield data

        for value in data.values():
            if isinstance(value, (dict, list)):
                yield from walk_dicts(value, depth + 1)

    elif isinstance(data, list):
        for value in data:
            if isinstance(value, (dict, list)):
                yield from walk_dicts(value, depth + 1)


def unique_dicts(items):
    result = []
    seen = set()

    for item in items:
        if not isinstance(item, dict):
            continue

        marker = json.dumps(item, sort_keys=True, default=str)

        if marker not in seen:
            seen.add(marker)
            result.append(item)

    return result


def normalize_rate(value):
    """Normalize a percentage stored as either 0-1 or 0-100."""
    number = as_float(value)

    if number is None:
        return None

    if 0 <= number <= 1:
        return number * 100

    if 0 <= number <= 100:
        return number

    return None


def ratio_from_counts(numerator, denominator):
    numerator = as_float(numerator)
    denominator = as_float(denominator)

    if numerator is None or denominator is None:
        return None

    if denominator <= 0 or numerator < 0 or numerator > denominator:
        return None

    return numerator / denominator * 100


def fmt_number(value, digits=2):
    if value is None:
        return "N/A"

    return f"{value:.{digits}f}"


def fmt_percent(value):
    if value is None:
        return "N/A"

    return f"{value:.1f}%"


# ============================================================
# STEAM PROFILE RESOLUTION
# ============================================================

def steam_id_from_input(value):
    value = value.strip()

    if not value:
        raise ValueError("Enter a Steam profile URL or SteamID64.")

    if value.isdigit():
        if len(value) >= 16:
            return value

        # A short numeric input may be a Deadlock account ID.
        return None

    match = re.search(
        r"steamcommunity\.com/profiles/(\d+)",
        value,
        re.I,
    )

    if match:
        return match.group(1)

    match = re.search(
        r"steamcommunity\.com/id/([^/?#]+)",
        value,
        re.I,
    )

    if match:
        return resolve_steam_vanity(match.group(1))

    if "://" not in value and "/" not in value:
        return resolve_steam_vanity(value)

    raise ValueError(
        "Enter a valid Steam profile URL, custom URL, or SteamID64."
    )


def resolve_steam_vanity(vanity):
    """Resolve a public Steam custom URL without a Steam API key."""
    vanity = urllib.parse.unquote(vanity.strip())

    if not vanity:
        raise ValueError("The Steam custom URL is empty.")

    xml_url = (
        f"{STEAM_BASE}/id/"
        f"{urllib.parse.quote(vanity)}/?xml=1"
    )

    try:
        xml_text = request_text(xml_url)
        root = ET.fromstring(xml_text)
        steam_id = root.findtext("steamID64")

        if steam_id and steam_id.isdigit():
            return steam_id

    except Exception:
        pass

    try:
        html = request_text(
            f"{STEAM_BASE}/id/{urllib.parse.quote(vanity)}/"
        )

        match = re.search(
            r'"steamid"\s*:\s*"(\d{17})"',
            html,
        )

        if match:
            return match.group(1)

        match = re.search(
            r"steamcommunity\.com/profiles/(\d{17})",
            html,
            re.I,
        )

        if match:
            return match.group(1)

    except Exception:
        pass

    raise ValueError(
        "Could not resolve this Steam custom URL. Check that the URL "
        "is correct and the profile is publicly accessible. You can "
        "also enter the 17-digit SteamID64 directly."
    )


def steam64_to_account_id(steam64):
    account_id = int(steam64) - STEAM_ID_BASE

    if account_id < 0:
        raise ValueError("The SteamID64 is invalid.")

    return account_id


def match_account_id(record):
    """Read an account identifier from common match metadata variants."""
    if not isinstance(record, dict):
        return None

    value = first_value(record, (
        "account_id", "accountId", "player_account_id", "playerAccountId",
        "steam_account_id", "steamAccountId", "deadlock_account_id",
        "deadlockAccountId", "account", "steam_id", "steamid",
        "steam_id64", "steamid64", "steamId64",
    ))

    if value is None:
        # Some responses nest identity under a player/user object.
        for key in ("player", "user", "profile", "identity", "account_info"):
            nested = record.get(key)
            if isinstance(nested, dict):
                value = match_account_id(nested)
                if value is not None:
                    break

    number = as_int(value)
    if number is None or number <= 0:
        return None
    if number >= STEAM_ID_BASE:
        number -= STEAM_ID_BASE
    return number if number > 0 else None


def merge_player_details(match_player, profile):
    """Merge profile fields without overwriting match-specific stats."""
    merged = dict(match_player)
    if isinstance(profile, dict):
        for key, value in profile.items():
            if value is not None and (key not in merged or merged[key] in (None, "")):
                merged[key] = value
    return merged


def steam_profile_url(account_id):
    if account_id is None:
        return ""

    steam64 = int(account_id) + STEAM_ID_BASE

    return f"{STEAM_BASE}/profiles/{steam64}/"


# ============================================================
# HERO STATISTICS AND CRITICAL-SHOT RATE
# ============================================================

CRIT_RATE_FIELDS = {
    "crit_shot_rate",
    "critshotrate",
    "critical_shot_rate",
    "criticalshotrate",
    "crit_rate",
    "critical_rate",
    "crit_shot_pct",
    "critical_shot_pct",
}

ACCURACY_FIELDS = {
    "accuracy",
    "accuracy_rate",
    "accuracy_pct",
    "hit_rate",
    "shot_accuracy",
    "shot_accuracy_pct",
}

CRIT_COUNT_FIELDS = {
    "crit_shots",
    "critical_shots",
    "num_crit_shots",
    "total_crit_shots",
    "critical_hits",
    "crit_hits",
}

SHOT_COUNT_FIELDS = {
    "total_shots",
    "shots_fired",
    "num_shots",
    "shots",
    "total_bullets_fired",
}

HIT_COUNT_FIELDS = {
    "shots_hit",
    "hits",
    "num_hits",
    "total_hits",
    "bullets_hit",
}


def extract_crit_rate(record):
    """
    Extract critical-shot rate from a hero-stat record.

    Prefer the API's direct crit_shot_rate field. If unavailable,
    calculate a percentage from compatible critical-shot and
    total-shot counts.
    """
    direct = get_field(record, CRIT_RATE_FIELDS)

    if direct is not None:
        return normalize_rate(direct)

    crit_count = get_field(record, CRIT_COUNT_FIELDS)
    shot_count = get_field(record, SHOT_COUNT_FIELDS)

    return ratio_from_counts(crit_count, shot_count)


def extract_accuracy(record):
    direct = get_field(record, ACCURACY_FIELDS)

    if direct is not None:
        return normalize_rate(direct)

    hits = get_field(record, HIT_COUNT_FIELDS)
    shots = get_field(record, SHOT_COUNT_FIELDS)

    return ratio_from_counts(hits, shots)


def aggregate_hero_stats(data):
    """
    Aggregate hero-stat accuracy and critical-shot rates.

    The API may provide direct percentages or underlying counts.
    Missing data remains None and is displayed as N/A, never 0%.
    """
    records = unique_dicts(list(walk_dicts(data)))

    accuracy_rates = []
    crit_rates = []

    accuracy_hits = 0.0
    accuracy_shots = 0.0
    crit_hits = 0.0
    crit_shots = 0.0

    accuracy_count_pairs = 0
    crit_count_pairs = 0

    for record in records:
        accuracy = extract_accuracy(record)
        crit_rate = extract_crit_rate(record)

        if accuracy is not None:
            accuracy_rates.append(accuracy)

        if crit_rate is not None:
            crit_rates.append(crit_rate)

        shots = as_float(get_field(record, SHOT_COUNT_FIELDS))
        hits = as_float(get_field(record, HIT_COUNT_FIELDS))
        crits = as_float(get_field(record, CRIT_COUNT_FIELDS))

        if (
            hits is not None
            and shots is not None
            and shots > 0
            and 0 <= hits <= shots
        ):
            accuracy_hits += hits
            accuracy_shots += shots
            accuracy_count_pairs += 1

        if (
            crits is not None
            and shots is not None
            and shots > 0
            and 0 <= crits <= shots
        ):
            crit_hits += crits
            crit_shots += shots
            crit_count_pairs += 1

    if accuracy_count_pairs:
        accuracy_result = ratio_from_counts(
            accuracy_hits,
            accuracy_shots,
        )
    elif accuracy_rates:
        accuracy_result = sum(accuracy_rates) / len(accuracy_rates)
    else:
        accuracy_result = None

    if crit_count_pairs:
        crit_result = ratio_from_counts(
            crit_hits,
            crit_shots,
        )
    elif crit_rates:
        crit_result = sum(crit_rates) / len(crit_rates)
    else:
        crit_result = None

    return {
        "accuracy": accuracy_result,
        "crit_rate": crit_result,
        "accuracy_records": len(accuracy_rates),
        "crit_records": len(crit_rates),
        "source_records": len(records),
    }


def get_hero_names():
    try:
        data = api_get("/v1/assets/heroes")
        names = {}

        for item in as_list(data):
            if not isinstance(item, dict):
                continue

            hero_id = first_value(
                item,
                ("id", "hero_id", "heroId"),
            )

            name = first_value(
                item,
                (
                    "name",
                    "hero_name",
                    "localized_name",
                    "display_name",
                ),
            )

            if hero_id is not None and name:
                names[str(hero_id)] = str(name)

        return names

    except Exception:
        return {}


def get_player_hero_stats(account_id):
    return api_get(
        "/v1/players/hero-stats",
        {"account_ids": str(account_id)},
    )


# ============================================================
# MATCH HISTORY AND SCREENING SCORE
# ============================================================

KILL_FIELDS = (
    "kills",
    "player_kills",
    "hero_kills",
    "num_kills",
)

DEATH_FIELDS = (
    "deaths",
    "player_deaths",
    "hero_deaths",
    "num_deaths",
)

ASSIST_FIELDS = (
    "assists",
    "player_assists",
    "num_assists",
)


def get_player_stat(match, field_names):
    if not isinstance(match, dict):
        return None

    value = first_value(match, field_names)

    if value is not None:
        return as_float(value)

    nested_names = (
        "player_stats",
        "playerStats",
        "stats",
        "player",
        "match_player",
        "player_data",
        "participant",
        "local_player",
    )

    for key in nested_names:
        nested = match.get(key)

        if isinstance(nested, dict):
            value = first_value(nested, field_names)

            if value is not None:
                return as_float(value)

    return None


def match_kda(match):
    kills = get_player_stat(match, KILL_FIELDS)
    deaths = get_player_stat(match, DEATH_FIELDS)
    assists = get_player_stat(match, ASSIST_FIELDS)

    if kills is None or deaths is None or assists is None:
        return None

    return (kills + assists) / max(deaths, 1)


def match_result(match):
    result = first_value(
        match,
        (
            "match_result",
            "result",
            "win",
            "won",
            "match_outcome",
        ),
    )

    if isinstance(result, bool):
        return result

    if isinstance(result, (int, float)):
        if result == 1:
            return True
        if result == 0:
            return False

    if isinstance(result, str):
        normalized = result.strip().lower()

        if normalized in (
            "win", "won", "victory", "true", "teamwin",
        ):
            return True

        if normalized in (
            "loss", "lose", "lost", "defeat", "false", "teamloss",
        ):
            return False

    team = first_value(
        match,
        ("team", "team_number", "team_id", "team_num"),
    )

    winner = first_value(
        match,
        (
            "winning_team",
            "winning_team_id",
            "winner",
            "winner_team",
        ),
    )

    if team is not None and winner is not None:
        try:
            return int(team) == int(winner)
        except (ValueError, TypeError):
            pass

    return None


# ============================================================
# FARMING / SOUL ANALYSIS
# ============================================================

FARM_NET_FIELDS = ("net_worth", "netWorth", "souls", "souls_collected")
FARM_LAST_HIT_FIELDS = ("last_hits", "lastHits", "creep_kills")
FARM_DENY_FIELDS = ("denies", "soul_denies", "soulDenies")
FARM_DURATION_FIELDS = ("match_duration_s", "duration_s", "matchDurationS", "duration")


def farming_match_stats(match):
    """Return available values only; do not treat missing stats as zero."""
    duration = get_player_stat(match, FARM_DURATION_FIELDS)
    if duration is None or duration <= 0:
        return None
    # API match-history duration fields are in seconds.
    minutes = duration / 60.0
    if minutes < 5 or minutes > 180:
        return None
    souls = get_player_stat(match, FARM_NET_FIELDS)
    hits = get_player_stat(match, FARM_LAST_HIT_FIELDS)
    denies = get_player_stat(match, FARM_DENY_FIELDS)
    values = {}
    for key, amount in (("souls", souls), ("last_hits", hits), ("denies", denies)):
        if amount is not None and amount >= 0:
            values[key] = amount
            values[key + "_per_min"] = amount / minutes
    return values if values else None


def analyze_farming(matches):
    """Match-level averages, consistency, and transparent data coverage."""
    records = [farming_match_stats(m) for m in matches if isinstance(m, dict)]
    records = [r for r in records if r is not None]
    result = {"matches_with_farming": len(records), "total_matches": len(matches)}
    import statistics
    for key in ("souls", "souls_per_min", "last_hits", "last_hits_per_min", "denies", "denies_per_min"):
        values = [r[key] for r in records if key in r]
        result[key + "_avg"] = statistics.mean(values) if values else None
        result[key + "_median"] = statistics.median(values) if values else None
        result[key + "_count"] = len(values)
    # A configurable, *illustrative* threshold, not a rank-calibrated benchmark.
    threshold = 1000.0
    rates = [r["souls_per_min"] for r in records if "souls_per_min" in r]
    result["high_soul_threshold"] = threshold
    result["high_soul_count"] = sum(v >= threshold for v in rates)
    result["high_soul_fraction"] = (result["high_soul_count"] / len(rates)) if rates else None
    result["recent_souls_per_min"] = (
        statistics.mean([r["souls_per_min"] for r in records[:10] if "souls_per_min" in r])
        if any("souls_per_min" in r for r in records[:10]) else None
    )
    # Keep farming as a separate contextual indicator; avoid an arbitrary
    # score penalty/bonus until hero-, rank- and mode-calibrated baselines exist.
    result["indicator"] = (
        "High sustained farming (review context)"
        if len(rates) >= 8 and result["high_soul_fraction"] >= 0.75
        else "Insufficient farming samples" if len(rates) < 8
        else "No sustained high-farming flag"
    )
    return result


def compare_farming_to_peers(history, account_id, max_matches=8):
    """Use real same-match opponents/teammates, not a fabricated global median.

    Match peers are a contextual skill-matched sample, not all Deadlock players.
    Failures never prevent a profile from loading.
    """
    matches = [m for m in as_list(history) if isinstance(m, dict)]
    chosen = []
    seen = set()
    for m in matches:
        match_id = as_int(first_value(m, ("match_id", "matchId")))
        if match_id is not None and match_id > 0 and match_id not in seen:
            chosen.append(match_id)
            seen.add(match_id)
        if len(chosen) >= max_matches:
            break

    def fetch(match_id):
        try:
            data = api_get(f"/v1/matches/{match_id}/metadata")
            players = find_match_players(data)
            match_info = data.get("match_info", {}) if isinstance(data, dict) else {}
            duration = get_player_stat(match_info, FARM_DURATION_FIELDS)
            if duration is None:
                duration = get_player_stat(data, FARM_DURATION_FIELDS)
            rows = []
            for player in players:
                pid = match_account_id(player)
                if pid is None or pid == account_id:
                    continue
                candidate = dict(player)
                if duration is not None and get_player_stat(candidate, FARM_DURATION_FIELDS) is None:
                    candidate["match_duration_s"] = duration
                stats = farming_match_stats(candidate)
                if stats and stats.get("souls_per_min") is not None:
                    rows.append((pid, stats["souls_per_min"]))
            return rows
        except Exception:
            return []

    peer_rates = []
    peer_accounts = set()
    matched_games = 0
    if chosen:
        with ThreadPoolExecutor(max_workers=2) as pool:
            for rows in pool.map(fetch, chosen):
                if rows:
                    matched_games += 1
                for pid, rate in rows:
                    peer_accounts.add(pid)
                    peer_rates.append(rate)

    player_rates = []
    for match in matches:
        stats = farming_match_stats(match)
        if stats and stats.get("souls_per_min") is not None:
            player_rates.append(stats["souls_per_min"])
    result = {
        "peer_median": statistics.median(peer_rates) if peer_rates else None,
        "peer_mean": statistics.mean(peer_rates) if peer_rates else None,
        "peer_observations": len(peer_rates),
        "peer_accounts": len(peer_accounts),
        "peer_matches": matched_games,
        "matches_checked": len(chosen),
        "player_median": statistics.median(player_rates) if player_rates else None,
        "difference_pct": None,
        "percentile": None,
        "status": "Unavailable",
    }
    if len(peer_rates) >= 20 and len(player_rates) >= 5:
        own = result["player_median"]
        benchmark = result["peer_median"]
        if benchmark and benchmark > 0:
            result["difference_pct"] = (own / benchmark - 1) * 100
            result["percentile"] = 100 * (sum(x < own for x in peer_rates) + .5 * sum(x == own for x in peer_rates)) / len(peer_rates)
            result["status"] = "Same-match peer comparison"
    elif chosen:
        result["status"] = "Insufficient peer match data"
    return result


def format_peer_comparison(peer):
    if not peer or peer.get("difference_pct") is None:
        return (f"Peer benchmark: {peer.get('status', 'Unavailable') if peer else 'Unavailable'} "
                f"({peer.get('peer_observations', 0) if peer else 0} observations / "
                f"{peer.get('peer_matches', 0) if peer else 0} matches)")
    return (f"Player median: {peer['player_median']:.0f} souls/min  |  "
            f"Same-match peers: {peer['peer_median']:.0f} souls/min  |  "
            f"Difference: {peer['difference_pct']:+.1f}%\n"
            f"Above ~{peer['percentile']:.0f}% of peer match observations  |  "
            f"{peer['peer_observations']} observations from {peer['peer_accounts']} players "
            f"across {peer['peer_matches']} matches")


def analyze_history(history):
    matches = [
        item for item in as_list(history)
        if isinstance(item, dict)
    ]

    all_kdas = [match_kda(item) for item in matches]
    valid_kdas = [
        value for value in all_kdas
        if value is not None
    ]

    recent_valid = []

    for match in matches:
        value = match_kda(match)

        if value is not None:
            recent_valid.append(value)

        if len(recent_valid) >= 10:
            break

    results = [match_result(item) for item in matches]
    valid_results = [
        value for value in results
        if value is not None
    ]

    average_kda = (
        sum(valid_kdas) / len(valid_kdas)
        if valid_kdas else None
    )

    recent_kda = (
        sum(recent_valid) / len(recent_valid)
        if recent_valid else None
    )

    win_rate = (
        sum(1 for value in valid_results if value)
        / len(valid_results) * 100
        if valid_results else None
    )

    farming = analyze_farming(matches)

    # Original raw scoring rules, maximum 70 points.
    raw_score = 0

    if (
        average_kda is not None
        and average_kda >= 5
        and len(valid_kdas) >= 5
    ):
        raw_score += 25

    if (
        recent_kda is not None
        and recent_kda >= 6
        and len(recent_valid) >= 5
    ):
        raw_score += 20

    if (
        win_rate is not None
        and win_rate >= 75
        and len(valid_results) >= 8
    ):
        raw_score += 25

    normalized_score = round(raw_score / 70 * 100)

    if raw_score < 20:
        risk = "Low"
    elif raw_score < 40:
        risk = "Moderate"
    elif raw_score < 60:
        risk = "Elevated"
    else:
        risk = "High"

    if len(matches) >= 30 and len(valid_results) >= 20:
        confidence = "High"
    elif len(matches) >= 10 and len(valid_results) >= 8:
        confidence = "Moderate"
    else:
        confidence = "Low"

    total_seconds = 0

    for match in matches:
        duration = as_float(
            first_value(
                match,
                (
                    "match_duration_s",
                    "duration_s",
                    "duration",
                ),
            )
        )

        if duration is not None and duration > 0:
            total_seconds += duration

    return {
        "matches": matches,
        "farming": farming,
        "match_count": len(matches),
        "average_kda": average_kda,
        "recent_kda": recent_kda,
        "valid_kda_count": len(valid_kdas),
        "recent_kda_count": len(recent_valid),
        "win_rate": win_rate,
        "valid_result_count": len(valid_results),
        "raw_score": raw_score,
        "score": normalized_score,
        "risk": risk,
        "confidence": confidence,
        "hours": total_seconds / 3600 if total_seconds else None,
    }


def extract_lifetime_matches(player, rank):
    """Only use explicit lifetime counters, never infer from returned history."""
    field_names = ("total_matches_played", "lifetime_matches_played", "total_matches", "matches_played", "match_count")
    for source in (player, rank):
        if not isinstance(source, dict):
            continue
        # Limit to top-level or clearly labeled lifetime/statistics containers.
        for record in (source, source.get("lifetime_stats"), source.get("career_stats"), source.get("stats")):
            if not isinstance(record, dict):
                continue
            for field in field_names:
                count = as_int(record.get(field))
                if count is not None and 0 < count < 1000000:
                    return count
    return None


def analyze_learning_progression(analysis):
    """Compare earliest and latest *available* matches, without inventing career history.

    Requires at least 16 matches with KDA and explicit timestamps. Chronology is
    derived from API timestamps, never assumed from the returned list order.
    """
    rows = []
    for match in analysis.get("matches", []):
        stamp = get_player_stat(match, ("start_time", "start_time_s", "start_time_timestamp", "match_start_time", "match_start_time_s", "startTime", "start_time_unix"))
        kda = match_kda(match)
        if stamp is not None and stamp > 1e9 and kda is not None:
            rows.append((stamp, kda))
    rows.sort(key=lambda x: x[0])
    result = {"status": "Unavailable (needs 16 timestamped matches)", "earlier_kda": None,
              "later_kda": None, "change_pct": None, "samples": len(rows), "career_start_confirmed": False}
    if len(rows) >= 16:
        n = min(10, len(rows) // 3)
        early = statistics.median(v for _, v in rows[:n])
        late = statistics.median(v for _, v in rows[-n:])
        result.update({"earlier_kda": early, "later_kda": late,
                       "change_pct": (late / early - 1) * 100 if early > 0 else None,
                       "status": "Earliest vs latest available games (not necessarily career start)"})
    return result


# The response.json supplied by the user is an UNFILTERED single-match
# distribution. Never describe its percentiles as player-level ranks.
# Persist user data outside the application and PyInstaller's extraction folder.
# On Windows, this resolves to %LOCALAPPDATA%\DeadlockProfileAnalyzer.
def get_app_data_dir():
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "DeadlockProfileAnalyzer"
    return Path.home() / ".local" / "share" / "DeadlockProfileAnalyzer"


APP_DATA_DIR = get_app_data_dir()
BENCHMARK_CACHE_PATH = APP_DATA_DIR / "general_benchmark.json"
LOG_DIR = APP_DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "analyzer.log"


def initialize_app_data():
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Preserve benchmarks downloaded by earlier app versions.
    legacy = Path.home() / ".deadlock_profile_analyzer" / "general_benchmark.json"
    if not BENCHMARK_CACHE_PATH.exists() and legacy.is_file():
        shutil.copy2(legacy, BENCHMARK_CACHE_PATH)


def configure_file_logging():
    initialize_app_data()
    from logging.handlers import RotatingFileHandler
    logger = logging.getLogger("deadlock")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
    return logger


APP_LOGGER = logging.getLogger("deadlock")


def report_uncaught_error(exc_type, exc_value, tb):
    APP_LOGGER.critical("Unhandled error", exc_info=(exc_type, exc_value, tb))
    try:
        sys.__excepthook__(exc_type, exc_value, tb)
    except Exception:
        pass
    try:
        messagebox.showerror("Deadlock Profile Analyzer — Error",
                             f"An error occurred. Diagnostic details are saved to:\n{LOG_FILE}\n\n{exc_value}")
    except Exception:
        pass


def report_thread_error(args):
    report_uncaught_error(args.exc_type, args.exc_value, args.exc_traceback)


def install_error_handlers():
    sys.excepthook = report_uncaught_error
    threading.excepthook = report_thread_error


BENCHMARK_URL_PATH = "/v1/analytics/player-stats/metrics"


def validate_general_benchmark(data):
    if not isinstance(data, dict):
        raise ValueError("Benchmark response must be a JSON object")
    for metric in ("net_worth_per_min", "kda", "accuracy", "crit_shot_rate"):
        node = data.get(metric)
        if not isinstance(node, dict) or not all(k in node for k in ("avg", "percentile50", "percentile90", "percentile95")):
            raise ValueError(f"Missing required benchmark fields for {metric}")
        values = [as_float(node[k]) for k in ("percentile50", "percentile90", "percentile95")]
        if any(v is None for v in values) or values != sorted(values):
            raise ValueError(f"Invalid percentile values for {metric}")
    return data


def load_general_benchmark():
    try:
        record = json.loads(BENCHMARK_CACHE_PATH.read_text(encoding="utf-8"))
        validate_general_benchmark(record["metrics"])
        return record
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_general_benchmark(data, source="imported", params=None):
    validate_general_benchmark(data)
    record = {"metrics": data, "source": source, "params": params or {},
              "retrieved_at": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
              "population": "single player-match observations; not player career percentiles"}
    BENCHMARK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BENCHMARK_CACHE_PATH.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def benchmark_match_percentile(value, node):
    """Interpolate the empirical single-match percentile thresholds; NOT player percentile."""
    points = []
    for pct in (1, 5, 10, 25, 50, 75, 90, 95, 99):
        v = as_float(node.get(f"percentile{pct}"))
        if v is not None:
            points.append((v, float(pct)))
    if not points or value is None:
        return None
    if value <= points[0][0]:
        return points[0][1]
    for (x0, p0), (x1, p1) in zip(points, points[1:]):
        if value <= x1:
            return p1 if x1 == x0 else p0 + (value - x0) / (x1 - x0) * (p1 - p0)
    return 99.0  # capped: distribution does not resolve above P99


def calculate_screening_v52(analysis, player=None, rank=None, benchmark_record=None):
    """Farming-first heuristic. Suspicion and confidence are separate outputs.

    Population benchmarks describe match rows; evaluate each match individually.
    Weights are design assumptions, not calibrated probabilities.
    """
    lifetime = extract_lifetime_matches(player, rank)
    analysis["lifetime_matches"] = lifetime
    analysis["match_count_source"] = "Reported lifetime total" if lifetime is not None else "Unavailable"
    matches = analysis.get("matches", [])
    farm = analysis.get("farming") or {}
    peer = analysis.get("peer_comparison") or {}
    record = benchmark_record or load_general_benchmark()
    metrics = record.get("metrics", {}) if isinstance(record, dict) else {}
    node = metrics.get("net_worth_per_min") or {}
    rates = [v["souls_per_min"] for m in matches if isinstance(m, dict)
             if (v := farming_match_stats(m)) and v.get("souls_per_min") is not None]
    match_percentiles = [benchmark_match_percentile(v, node) for v in rates] if node else []
    match_percentiles = [v for v in match_percentiles if v is not None]
    high90 = sum(v >= 90 for v in match_percentiles)
    high95 = sum(v >= 95 for v in match_percentiles)
    farm["benchmark_matches"] = len(match_percentiles)
    farm["benchmark_p90_matches"] = high90
    farm["benchmark_p95_matches"] = high95
    farm["benchmark_p90_fraction"] = high90 / len(match_percentiles) if match_percentiles else None
    farm["benchmark_median_match_percentile"] = statistics.median(match_percentiles) if match_percentiles else None
    # Benchmark score: sustained exceptional match performance, not career percentile.
    benchmark_farm = None
    if match_percentiles:
        benchmark_farm = max(0, min(100, 0.65 * (statistics.median(match_percentiles)-50)*2
                                     + 0.35 * 100 * high90 / len(match_percentiles)))
    peer_farm = None
    if peer.get("difference_pct") is not None:
        peer_farm = max(0, min(100, peer["difference_pct"] / 45 * 100))
    # Same-match peer comparison can function even when the general benchmark is absent.
    if benchmark_farm is not None and peer_farm is not None:
        farming_score = 0.70 * benchmark_farm + 0.30 * peer_farm
    else:
        farming_score = benchmark_farm if benchmark_farm is not None else peer_farm
    # Legacy 70-point combat score is not part of the active calibration.
    combat = None
    kdas = [match_kda(m) for m in matches if isinstance(m, dict)]
    kdas = [v for v in kdas if v is not None]
    kda_node = metrics.get("kda", {})
    combat_pct = [benchmark_match_percentile(v, kda_node) for v in kdas] if kda_node else []
    combat_pct = [v for v in combat_pct if v is not None]
    if combat_pct:
        combat = min(100, max(0, statistics.median(combat_pct)-50)*2)
    # No fabricated mechanical accuracy when per-match counters are unavailable.
    mechanics = None
    accuracy_node = metrics.get("accuracy", {})
    acc_values = []
    for m in matches:
        if not isinstance(m, dict):
            continue
        acc = get_player_stat(m, ("accuracy", "accuracy_rate"))
        if acc is None:
            hit = get_player_stat(m, ("shots_hit",))
            missed = get_player_stat(m, ("shots_missed",))
            if hit is not None and missed is not None and hit + missed > 0:
                acc = hit / (hit + missed)
        if acc is not None:
            acc = acc / 100 if acc > 1 else acc
            if 0 <= acc <= 1:
                acc_values.append(acc)
    if accuracy_node and acc_values:
        acc_pcts = [benchmark_match_percentile(v, accuracy_node) for v in acc_values]
        mechanics = min(100, max(0, (statistics.median(acc_pcts)-50)*2))
    # Consistency is only scored when we have several observed matches.
    consistency = (100 * high90 / len(match_percentiles)) if len(match_percentiles) >= 3 else None
    # Experience mismatch is supported only by API-reported LIFETIME match counts.
    # A returned history count does not establish lifetime Deadlock experience.
    experience = None
    if lifetime is not None and lifetime >= 0 and farming_score is not None:
        experience = max(0, min(100, farming_score * max(0, (400 - lifetime) / 400)))
    # Do not replace missing components with zero: reweight observed components.
    components = ((40, farming_score), (25, experience), (20, combat),
                  (10, mechanics), (5, consistency))
    available = [(weight, value) for weight, value in components if value is not None]
    score = round(sum(w * v for w, v in available) / sum(w for w, _ in available)) if available else 0
    score = max(0, min(100, score))
    risk = "High" if score >= 75 else "Elevated" if score >= 55 else "Moderate" if score >= 30 else "Low"
    observed = max(len(match_percentiles), len(kdas), analysis.get("valid_result_count", 0))
    confidence = "High" if observed >= 30 else "Moderate" if observed >= 10 else "Low"
    if not record and confidence == "High":
        confidence = "Moderate"
    if lifetime is None and confidence == "High":
        confidence = "Moderate"
    analysis.update({
        "score": score, "risk": risk, "confidence": confidence,
        "combat_component": round(combat, 1) if combat is not None else None,
        "farming_component": round(farming_score, 1) if farming_score is not None else None,
        "mechanics_component": round(mechanics, 1) if mechanics is not None else None,
        "consistency_component": round(consistency, 1) if consistency is not None else None,
        "experience_component": round(experience, 1) if experience is not None else None,
        "performance_component": round(score, 1), "evidence_factor": 1.0,
        "experience_factor": 1.0, "experience_expectation": None,
        "experience_label": ("Lifetime experience unavailable; no low-experience boost" if lifetime is None
                             else f"API-reported lifetime matches: {lifetime:,}"),
        "learning_progression": analyze_learning_progression(analysis),
        "benchmark_source": record.get("source", "local") if record else "Unavailable",
        "benchmark_coverage": len(match_percentiles),
        "score_version": "0.5.8", "screening_note": (
            "Farming-first heuristic; confidence is separate from suspicion. "
            "Benchmark percentiles describe single matches, NOT player percentiles. "
            "Unadjusted for hero, patch, match duration, or matchmaking strength; not proof of smurfing."),
    })
    return analysis


def calculate_screening_v50(analysis, player=None, rank=None):
    """Compatibility wrapper; v0.5.8 scoring is used."""
    return calculate_screening_v52(analysis, player, rank)


# Backward-compatible alias for callers relying on the old function name.
def calculate_screening_v49(analysis, player=None, rank=None):
    return calculate_screening_v50(analysis, player, rank)


def get_player(account_id):
    data = api_get(
        "/v1/players/steam",
        {"account_ids": str(account_id)},
    )

    players = as_list(data)

    return (
        players[0]
        if players and isinstance(players[0], dict)
        else {}
    )


def get_rank(account_id):
    try:
        return api_get(f"/v1/players/{account_id}/rank")
    except Exception as exc:
        return {"error": str(exc)}


def get_history(account_id):
    data = api_get(
        f"/v1/players/{account_id}/match-history"
    )
    return as_list(data)


# ============================================================
# TOP HERO AGGREGATION
# ============================================================

def get_top_heroes(hero_data, hero_names):
    """
    Read hero usage records and calculate wins, losses and win rate.

    Supports the documented matches_played field as well as
    alternative field names used in different response formats.
    """
    rows = []

    for item in as_list(hero_data):
        if not isinstance(item, dict):
            continue

        hero_id = first_value(
            item,
            ("hero_id", "heroId", "id"),
        )

        if hero_id is None:
            continue

        matches_value = first_value(
            item,
            (
                "matches_played",
                "matchesPlayed",
                "matches",
                "match_count",
                "games_played",
            ),
        )

        # Some formats provide an array of match IDs rather than a count.
        if isinstance(matches_value, list):
            matches = len(matches_value)
        else:
            matches = as_int(matches_value, 0) or 0

        wins = as_int(
            first_value(
                item,
                ("wins", "win_count", "games_won"),
            ),
            0,
        ) or 0

        wins = max(0, wins)

        if matches > 0:
            wins = min(wins, matches)
            losses = max(0, matches - wins)
            win_rate = wins / matches * 100
        else:
            losses = None
            win_rate = None

        hero_name = hero_names.get(
            str(hero_id),
            f"Hero {hero_id}",
        )

        rows.append({
            "hero_id": hero_id,
            "name": hero_name,
            "matches": matches,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
        })

    rows.sort(
        key=lambda item: (
            item["matches"],
            item["wins"],
        ),
        reverse=True,
    )

    return rows[:5]


# ============================================================
# REPORT GENERATION
# ============================================================

def build_smurf_report_description(analysis, player=None, account_id=None):
    """Create a concise, evidence-based, paste-ready player report description.

    This is a screening summary, not an assertion that smurfing is proven.
    """
    if not isinstance(analysis, dict):
        return ""
    farm = analysis.get("farming") or {}
    peer = analysis.get("peer_comparison") or {}
    reasons = []
    median_rate = as_float(farm.get("souls_per_min_median"))
    if median_rate is not None:
        reasons.append(f"a median of {median_rate:,.0f} souls per minute across available matches")
    n = int(farm.get("benchmark_matches") or 0)
    p90 = int(farm.get("benchmark_p90_matches") or 0)
    p95 = int(farm.get("benchmark_p95_matches") or 0)
    if n > 0:
        reasons.append(f"farming above the general single-match P90 benchmark in {p90}/{n} matches"
                       + (f" (P95 in {p95}/{n})" if p95 else ""))
    peer_diff = as_float(peer.get("difference_pct"))
    if peer_diff is not None and peer_diff > 0:
        reasons.append(f"farming {peer_diff:.1f}% above the same-match peer comparison")
    lifetime = analysis.get("lifetime_matches")
    if isinstance(lifetime, (int, float)) and not isinstance(lifetime, bool):
        if lifetime < 100:
            reasons.append(f"this performance despite only {int(lifetime):,} API-reported lifetime matches")
        elif lifetime < 400:
            reasons.append(f"{int(lifetime):,} API-reported lifetime matches")
    kda = as_float(analysis.get("average_kda"))
    if kda is not None and kda >= 5:
        reasons.append(f"an average KDA of {kda:.2f} across the available match history")
    if not reasons:
        reasons.append("unusual performance patterns in the available match history")
    # Use no more than four concrete observations so the report remains paste-friendly.
    details = "; ".join(reasons[:4])
    confidence = analysis.get("confidence", "Unknown")
    matches = analysis.get("match_count", 0)
    return ("Player is suspected of smurfing due to " + details + ". "
            + f"Analyzer screening: {analysis.get('score', 'N/A')}/100 "
            + f"({analysis.get('risk', 'Unknown')} suspicion; {confidence} evidence confidence; "
            + f"{matches} matches returned). "
            + "These are indicators of possible prior experience, not definitive proof. "
            + "Please review the account and match history.")


def build_report(
    account_id,
    player,
    rank,
    analysis,
    stats,
    top_heroes,
):
    name = first_value(
        player,
        (
            "personaname",
            "persona_name",
            "name",
            "player_name",
        ),
    ) or f"Account {account_id}"

    lines = [
        "DEADLOCK PLAYER PROFILE REPORT",
        "=" * 48,
        f"Player: {name}",
        f"Deadlock Account ID: {account_id}",
        f"Steam Profile: {steam_profile_url(account_id)}",
        "",
        "SMURF SCREENING",
        "-" * 48,
        f"Normalized screening score: {analysis['score']}/100",
        
        f"Combat anomaly component: {fmt_number(analysis.get('combat_component'))}/100",
        f"Farming anomaly component: {fmt_number(analysis.get('farming_component'))}/100 (N/A = excluded)",
        f"Performance component: {fmt_number(analysis.get('performance_component'))}/100",
        f"Evidence confidence (separate): {analysis['confidence']}",
        f"Experience mismatch component: {fmt_number(analysis.get('experience_component'))}/100",
        f"Mechanical component: {fmt_number(analysis.get('mechanics_component'))}/100",
        f"Consistency component: {fmt_number(analysis.get('consistency_component'))}/100",
        f"General benchmark source: {analysis.get('benchmark_source')}",
        f"Matches compared to benchmark: {analysis.get('benchmark_coverage', 0)}",
        f"Lifetime matches (API reported): {analysis.get('lifetime_matches') if analysis.get('lifetime_matches') is not None else 'Unavailable'}",
        f"Experience context: {analysis.get('experience_label', 'Unavailable')}",
        f"Experience expectation index (model assumption): {fmt_number(analysis.get('experience_expectation'), 3)}",
        f"Learning progression: {analysis.get('learning_progression', {}).get('status', 'Unavailable')}",
        f"Earlier available median KDA: {fmt_number(analysis.get('learning_progression', {}).get('earlier_kda'))}",
        f"Later available median KDA: {fmt_number(analysis.get('learning_progression', {}).get('later_kda'))}",
        f"Risk category: {analysis['risk']}",
        f"Confidence: {analysis['confidence']}",
        "",
        "PERFORMANCE",
        "-" * 48,
        f"Matches returned: {analysis['match_count']}",
        f"Win rate: {fmt_percent(analysis['win_rate'])}",
        f"Average KDA: {fmt_number(analysis['average_kda'])}",
        f"Recent KDA: {fmt_number(analysis['recent_kda'])}",
        f"Matches with KDA data: {analysis['valid_kda_count']}",
        f"Recent matches with KDA data: {analysis['recent_kda_count']}",
        f"Recorded hours: {fmt_number(analysis['hours'])}",
        "",
        "FARMING / SOUL ECONOMY",
        "-" * 48,
        f"Average final net worth: {fmt_number(analysis['farming']['souls_avg'], 0)} souls",
        f"Average net worth per minute: {fmt_number(analysis['farming']['souls_per_min_avg'], 1)}",
        f"Median net worth per minute: {fmt_number(analysis['farming']['souls_per_min_median'], 1)}",
        f"Recent net worth per minute: {fmt_number(analysis['farming']['recent_souls_per_min'], 1)}",
        f"Average last hits: {fmt_number(analysis['farming']['last_hits_avg'], 1)}",
        f"Last hits per minute: {fmt_number(analysis['farming']['last_hits_per_min_avg'], 2)}",
        f"Average soul denies: {fmt_number(analysis['farming']['denies_avg'], 1)}",
        f"Soul denies per minute: {fmt_number(analysis['farming']['denies_per_min_avg'], 2)}",
        f"High farming frequency (>= {analysis['farming']['high_soul_threshold']:.0f} souls/min): "
        + (fmt_percent(analysis['farming']['high_soul_fraction'] * 100) if analysis['farming']['high_soul_fraction'] is not None else 'N/A'),
        f"Farming indicator: {analysis['farming']['indicator']}",
        "",
        "SAME-MATCH FARMING BENCHMARK (NOT GLOBAL)",
        format_peer_comparison(analysis.get("peer_comparison")),
        "Peers are other players from the analyzed player's recent matches.",
        "This is not an all-player median or a rank-wide population estimate.",
        f"Net worth coverage: {analysis['farming']['souls_count']}/{analysis['match_count']} matches",
        f"Last-hit coverage: {analysis['farming']['last_hits_count']}/{analysis['match_count']} matches",
        f"Deny coverage: {analysis['farming']['denies_count']}/{analysis['match_count']} matches",
        "Farming contributes to the v5.0 screening score only with adequate same-match peer data.",
        "Final net worth is not the same as lifetime souls earned or lane-only farm.",
        "",
        "AIM STATISTICS",
        "-" * 48,
        f"Accuracy: {fmt_percent(stats['accuracy'])}",
        f"Critical-shot rate: {fmt_percent(stats['crit_rate'])}",
        f"Accuracy records found: {stats['accuracy_records']}",
        f"Critical-shot records found: {stats['crit_records']}",
        f"Hero-stat records inspected: {stats['source_records']}",
        "",
        "TOP HEROES",
        "-" * 48,
    ]

    if top_heroes:
        for hero in top_heroes:
            if hero["losses"] is None:
                record = f"{hero['wins']} wins"
                rate = "Win rate N/A"
            else:
                record = f"{hero['wins']}W / {hero['losses']}L"
                rate = fmt_percent(hero["win_rate"])

            lines.append(
                f"{hero['name']} — {hero['matches']} matches, "
                f"{record}, {rate}"
            )
    else:
        lines.append("No hero usage data was returned.")

    lines.extend([
        "",
        "RANK RESPONSE",
        "-" * 48,
        json.dumps(rank, indent=2, ensure_ascii=False),
        "",
        "LIMITATIONS",
        "-" * 48,
        "This screening score is a heuristic, not proof of smurfing.",
        "The v0.5.8 score emphasizes farming; evidence confidence never multiplies suspicion.",
        "Lifetime experience is never inferred from returned history; unavailable means unknown.",
        "Benchmark percentiles represent individual match rows, not career/player percentiles.",
        "Peer comparison is from shared matches, not a global or rank-wide benchmark.",
        "Scoring weights and experience mismatch curve are heuristic assumptions, not calibrated probabilities.",
        "Missing aim statistics are shown as N/A rather than 0%.",
    ])

    return "\n".join(lines)


# ============================================================
# MATCH METADATA PARSING
# ============================================================

def find_match_players(data, depth=0):
    if depth > 12:
        return []

    preferred_keys = (
        "players",
        "match_players",
        "player_stats",
        "participants",
        "player_metadata",
        "match_info",
        "metadata",
        "data",
        "match",
    )

    if isinstance(data, dict):
        for key in preferred_keys:
            value = data.get(key)

            if isinstance(value, (dict, list)):
                found = find_match_players(value, depth + 1)

                if found:
                    return found

        normalized = {
            normalized_key(key)
            for key in data.keys()
        }

        identity_keys = {
            normalized_key(key)
            for key in (
                "account_id",
                "accountId",
                "player_account_id",
                "steam_id",
                "steamid",
                "player_name",
                "personaname",
            )
        }

        stat_keys = {
            normalized_key(key)
            for key in (
                "hero_id",
                "kills",
                "deaths",
                "assists",
                "player_kills",
                "player_deaths",
                "player_assists",
            )
        }

        if normalized & identity_keys:
            return [data]

        if len(normalized & stat_keys) >= 2:
            return [data]

        result = []

        for value in data.values():
            if isinstance(value, (dict, list)):
                result.extend(
                    find_match_players(value, depth + 1)
                )

        return unique_dicts(result)

    if isinstance(data, list):
        result = []

        for item in data:
            result.extend(find_match_players(item, depth + 1))

        return unique_dicts(result)

    return []


# ============================================================
# GUI APPLICATION
# ============================================================


# ============================================================
# RANK BENCHMARKS — single player-match distributions
# ============================================================

RANK_BENCHMARK_TARGET = 500
RANK_BENCHMARK_FIELDS = (
    ("Souls/min", ("net_worth_per_min", "networth_per_min", "souls_per_min")),
    ("LH/min", ("last_hits_per_min", "last_hits_per_minute")),
    ("Denies/min", ("denies_per_min", "denies_per_minute")),
    ("KDA", ("kda", "kda_ratio")),
    ("Accuracy", ("accuracy", "accuracy_rate")),
    ("Crit rate", ("crit_shot_rate", "critical_shot_rate", "crit_rate")),
)


def benchmark_metric_node(data, names):
    """Only accept explicitly named metric objects; never substitute a different stat."""
    if not isinstance(data, dict):
        return None
    sources = [data]
    for key in ("metrics", "stats", "data", "player_stats"):
        if isinstance(data.get(key), dict):
            sources.append(data[key])
    for source in sources:
        for name in names:
            value = source.get(name)
            if isinstance(value, dict):
                return value
    return None


def benchmark_stat_value(node, kind="avg"):
    if not isinstance(node, dict):
        return None
    aliases = {
        "avg": ("avg", "average", "mean"),
        "p50": ("percentile50", "p50", "median", "q50"),
        "p75": ("percentile75", "p75", "q75"),
        "p90": ("percentile90", "p90", "q90"),
    }
    for key in aliases[kind]:
        if key in node:
            return as_float(node[key])
    for group in ("percentiles", "quantiles"):
        sub = node.get(group)
        if isinstance(sub, dict):
            for key in aliases[kind]:
                if key in sub:
                    return as_float(sub[key])
    return None


def benchmark_sample_count(data):
    if not isinstance(data, dict):
        return None
    for key in ("total_matches", "match_count", "matches", "num_matches", "n_matches"):
        value = as_int(data.get(key))
        if value is not None and value >= 0:
            return value
    return None


def benchmark_rank_rows():
    """The API encodes badge tier in tens and subtier in the last digit."""
    # Ranks are read from metadata where possible. These names are only fallbacks.
    try:
        metadata = api_get("/v1/assets/ranks")
    except Exception:
        metadata = None
    mapping = {}
    if isinstance(metadata, dict):
        items = metadata.get("ranks") or metadata.get("data") or metadata
        if isinstance(items, dict):
            for key, item in items.items():
                if isinstance(item, dict):
                    tier = as_int(item.get("tier") or item.get("rank") or key)
                    label = item.get("name") or item.get("title") or item.get("display_name")
                    if tier is not None and label:
                        mapping[tier] = str(label)
        elif isinstance(items, list):
            metadata = items
    if isinstance(metadata, list):
        for item in metadata:
            if isinstance(item, dict):
                tier = as_int(item.get("tier") or item.get("rank") or item.get("id"))
                label = item.get("name") or item.get("title") or item.get("display_name")
                if tier is not None and label:
                    mapping[tier] = str(label)
    return [(tier, mapping.get(tier, f"Tier {tier} (name unavailable)")) for tier in range(1, 12)]


def fetch_rank_benchmark(tier, days=30):
    import time
    params = {
        "game_mode": "normal", "match_mode": "ranked,unranked",
        "min_average_badge": tier * 10,
        "max_average_badge": tier * 10 + 6,
        "max_matches": RANK_BENCHMARK_TARGET,
        "min_unix_timestamp": int(time.time()) - int(days) * 86400,
    }
    data = api_get("/v1/analytics/player-stats/metrics", params)
    if not isinstance(data, dict):
        raise ValueError("Unexpected player metrics response type")
    return data


class InstantTooltip:
    """Show a contextual explanation immediately on mouse entry, without timers."""
    def __init__(self, widget, content):
        self.widget = widget
        self.content = content
        self.popup = None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<Destroy>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def show(self, event=None):
        self.hide()
        try:
            message = self.content() if callable(self.content) else self.content
            if not message:
                return
            self.popup = tk.Toplevel(self.widget)
            self.popup.wm_overrideredirect(True)
            self.popup.configure(bg="#dcb978")
            frame = tk.Frame(self.popup, bg="#203032", padx=13, pady=10)
            frame.pack(padx=1, pady=1)
            tk.Label(frame, text=message, bg="#203032", fg="#f4ebd8",
                     font=("Segoe UI", 10), justify="left", anchor="w",
                     wraplength=470).pack()
            self.popup.update_idletasks()
            x = self.widget.winfo_pointerx() + 14
            y = self.widget.winfo_pointery() + 18
            sw = self.widget.winfo_screenwidth()
            sh = self.widget.winfo_screenheight()
            x = max(0, min(x, sw - self.popup.winfo_reqwidth() - 10))
            y = max(0, min(y, sh - self.popup.winfo_reqheight() - 10))
            self.popup.wm_geometry(f"+{x}+{y}")
        except tk.TclError:
            self.popup = None

    def hide(self, event=None):
        popup, self.popup = self.popup, None
        if popup is not None:
            try:
                popup.destroy()
            except tk.TclError:
                pass


class GuiStream:
    """Forward stdout/stderr to the GUI without calling Tk from worker threads."""
    def __init__(self, target_queue, level, original=None):
        self.target_queue = target_queue
        self.level = level
        self.original = original
        self.encoding = "utf-8"

    def write(self, value):
        if value:
            self.target_queue.put((self.level, str(value)))
            if self.original is not None:
                try:
                    self.original.write(value)
                except Exception:
                    pass
            if self.level == "error":
                APP_LOGGER.error("stderr: %s", str(value).rstrip())
        return len(value) if value else 0

    def flush(self):
        if self.original is not None:
            try:
                self.original.flush()
            except Exception:
                pass

class DeadlockAnalyzer(tk.Tk):
    BG = "#10191c"
    PANEL = "#1c292b"
    PANEL_ALT = "#283b3c"
    TEXT = "#f4ebd8"
    MUTED = "#b3b7ab"
    ACCENT = "#dcb978"
    GREEN = "#83c4ad"
    YELLOW = "#e6b56d"
    RED = "#e88e7f"

    RISK_STYLES = {
        "Low": {
            "label": "LOW RISK",
            "background": "#24443b",
            "foreground": "#a3dfc1",
        },
        "Moderate": {
            "label": "MODERATE RISK",
            "background": "#53452b",
            "foreground": "#f4d596",
        },
        "Elevated": {
            "label": "ELEVATED RISK",
            "background": "#613e2d",
            "foreground": "#efb282",
        },
        "High": {
            "label": "HIGH RISK",
            "background": "#5c3134",
            "foreground": "#f3a4a4",
        },
    }

    def __init__(self):
        super().__init__()

        self.title("Deadlock • Profile Intelligence v0.5.8")
        # Include the app icon in the distribution without depending on the current directory.
        try:
            icon_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "deadlock_analyzer.ico"
            if icon_path.is_file():
                self.iconbitmap(default=str(icon_path))
        except (OSError, tk.TclError):
            pass

        self.geometry("1160x850")
        self.minsize(900, 650)
        self.configure(bg=self.BG)

        self.current_account_id = None
        self.current_analysis = None
        self.current_player = {}
        self.current_rank = {}
        self.current_stats = {}
        self.current_top_heroes = []
        self.current_report = ""
        self.hero_names = {}
        self.match_raw = None
        self.rank_benchmark_rows = {}
        self.rank_benchmark_loading = False
        self.general_benchmark_loading = False
        self.indicator_rows = {}

        self.console_queue = queue.Queue()
        self.configure_styles()
        self.build_ui()
        self.install_console_capture()
        self.after(80, self.drain_console_queue)
        self.report_callback_exception = lambda exc, value, tb: report_uncaught_error(exc, value, tb)
        # Cache is loaded immediately by the benchmark tab; refresh online without blocking UI.
        self.after(200, self.start_general_benchmark_fetch)

    # --------------------------------------------------------
    # Styling
    # --------------------------------------------------------

    @staticmethod
    def confidence_explanation(analysis):
        if not analysis:
            return "Evidence confidence: how much match history supports the screening result. Analyze a profile for specific reasons."
        n = max(analysis.get("benchmark_coverage", 0),
                analysis.get("valid_kda_count", 0),
                analysis.get("valid_result_count", 0))
        confidence = analysis.get("confidence", "Unknown")
        lifetime = analysis.get("lifetime_matches")
        source = analysis.get("benchmark_source", "Unavailable")
        lines = [f"EVIDENCE CONFIDENCE: {confidence}",
                 f"{n} usable match observations (from returned history).",
                 "Low: fewer than 10; Moderate: 10–29; High: 30+."]
        if lifetime is None:
            lines.append("Lifetime match count unavailable; returned matches are not lifetime matches.")
        else:
            lines.append(f"API-reported lifetime matches: {lifetime:,}.")
        if source == "Unavailable":
            lines.append("General benchmark unavailable; benchmark-based comparisons may be missing.")
        if confidence == "Moderate" and n >= 30:
            lines.append("High is capped at Moderate when lifetime data or benchmark is missing.")
        lines.append("Confidence measures evidence coverage, NOT smurf likelihood. It does not reduce suspicion.")
        return "\n".join(lines)

    @staticmethod
    def help_text(key):
        definitions = {
            "score": "SMURF SCREENING (0–100)\nA heuristic rating of suspicious gameplay, weighted toward farming.\nIt is not a percentage chance of smurfing and is not proof. Missing components are excluded and remaining components reweighted.",
            "risk": "SCREENING LEVEL\nLow: 0–29 | Moderate: 30–54 | Elevated: 55–74 | High: 75–100.\nThis is a suspicion category, not an evidence-confidence category.",
            "win_rate": "WIN RATE\nShare of returned matches with a known win/loss result that were won. Not necessarily lifetime win rate.",
            "average_kda": "AVERAGE KDA\nKills and assists relative to deaths across available match records. A high KDA alone is not proof of smurfing.",
            "recent_kda": "RECENT KDA\nKDA for the recent portion of the returned match history, not necessarily the player's lifetime performance.",
            "match_count": "MATCHES RETURNED\nNumber of matches the API returned for this analysis. This is NOT a verified lifetime match count or total hours.",
            "accuracy": "ACCURACY\nReported shots hit divided by shots fired when counters are available. Hero weapons and abilities can affect comparisons.",
            "crit": "CRITICAL SHOT RATE\nReported fraction of shots that were critical hits when data is available. Hero-specific mechanics can influence this metric.",
            "player": "PLAYER MEDIAN SOULS/MIN\nMedian economic income per minute across the player's returned matches with usable soul data. Not their best game.",
            "peer": "PEER MEDIAN SOULS/MIN\nMedian income per minute for other players in the same observed matches. This is NOT a global or rank-matched population benchmark.",
            "difference": "DIFFERENCE VS PEERS\nRelative difference between the player's median souls/min and the observed same-match peer median. Large positive values are an important farming signal, but match context matters.",
            "last_hits": "AVERAGE LAST HITS\nAverage lane-creep last hits in returned matches with available stats. Total per match, not per minute.",
            "denies": "AVERAGE DENIES\nAverage enemy soul denies per returned match with available stats. Total per match, not per minute.",
            "consistency": "HIGH SOUL-RATE GAMES\nShare of usable matches exceeding the analyzer's provisional high souls/min threshold. This is NOT a player percentile.",
            "farming_details": "FARMING DETAILS\nSouls/min normalizes economy by match duration. 'Soul data' counts usable records. The high-rate threshold is provisional. Same-match peers and population benchmarks are different comparisons.",
            "screening_details": "SCORING COMPONENTS\nFarming, combat, mechanics, consistency and verified experience mismatch contribute when available. Benchmark P90 counts single-match performances, NOT a player's global percentile. Missing experience data is not treated as zero experience.",
            "indicators": "SCREENING INDICATORS\nObserved performance datapoints that may warrant review. They are clues, not confirmed rule violations. Some observations can reflect the same underlying performance and should not be counted as independent proof.",
        }
        return definitions.get(key, "No explanation available.")

    def add_help(self, widget, key=None, content=None):
        InstantTooltip(widget, content if content is not None else lambda: self.help_text(key))
        try:
            widget.configure(cursor="question_arrow")
        except tk.TclError:
            pass

    def configure_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(
            ".",
            background=self.BG,
            foreground=self.TEXT,
            fieldbackground=self.PANEL,
            bordercolor=self.PANEL_ALT,
            font=("Segoe UI", 10),
        )

        style.configure("TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)

        style.configure(
            "TLabel",
            background=self.BG,
            foreground=self.TEXT,
        )

        style.configure(
            "Muted.TLabel",
            background=self.BG,
            foreground=self.MUTED,
        )

        style.configure(
            "Panel.TLabel",
            background=self.PANEL,
            foreground=self.TEXT,
        )

        style.configure(
            "TButton",
            padding=7,
            background=self.PANEL_ALT,
            foreground=self.TEXT,
        )

        style.map(
            "TButton",
            background=[("active", "#405654")],
        )

        style.configure("Treeview", background=self.PANEL, fieldbackground=self.PANEL, foreground=self.TEXT, rowheight=28, borderwidth=0)
        style.configure("Treeview.Heading", background=self.PANEL_ALT, foreground=self.ACCENT, font=("Segoe UI Semibold", 10), relief="flat")
        style.map("Treeview", background=[("selected", "#48635c")], foreground=[("selected", self.TEXT)])
        style.configure(
            "TEntry",
            fieldbackground=self.PANEL,
            foreground=self.TEXT,
        )

        style.configure(
            "TNotebook",
            background=self.BG,
            borderwidth=0,
        )

        style.configure(
            "TNotebook.Tab",
            padding=(14, 8),
            background=self.PANEL,
            foreground=self.TEXT,
        )

        style.map(
            "TNotebook.Tab",
            background=[("selected", self.PANEL_ALT)],
            foreground=[("selected", self.TEXT)],
            # Selected tab receives the larger padding; inactive tabs stay compact.
            padding=[("selected", (20, 12)), ("!selected", (14, 7))],
        )

    def paint_header_gradient(self, event=None):
        canvas = self.hero_banner
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        left, right = (17, 37, 39), (55, 68, 58)
        for x in range(0, width, 3):
            t = x / max(width - 1, 1)
            rgb = tuple(round(a * (1-t) + b*t) for a, b in zip(left, right))
            canvas.create_rectangle(x, 0, x+3, height, fill="#%02x%02x%02x" % rgb, outline="")
        canvas.create_rectangle(0, 0, 5, height, fill=self.ACCENT, outline="")
        canvas.create_text(23, 30, anchor="w", text="DEADLOCK", fill=self.TEXT,
                           font=("Georgia", 27, "bold"))
        canvas.create_text(24, 72, anchor="w", text="P R O F I L E   I N T E L L I G E N C E   •   0.5.8",
                           fill=self.ACCENT, font=("Segoe UI", 10, "bold"))
        canvas.create_line(24, 89, width-24, 89, fill="#75836b", width=1)

    # --------------------------------------------------------
    # Main interface
    # --------------------------------------------------------

    def build_ui(self):
        # Native Canvas gradient: no Pillow or other dependencies required.
        self.hero_banner = tk.Canvas(self, height=102, bg=self.BG, highlightthickness=0)
        self.hero_banner.pack(fill="x", padx=18, pady=(15, 2))
        self.hero_banner.bind("<Configure>", self.paint_header_gradient)
        header = tk.Frame(self, bg=self.BG)
        header.pack(fill="x", padx=18, pady=(2, 8))

        tk.Label(
            header,
            text="DEADLOCK  /  PROFILE INTELLIGENCE",
            font=("Segoe UI", 20, "bold"),
            bg=self.BG,
            fg=self.TEXT,
        ).pack(anchor="w")

        tk.Label(
            header,
            text=(
                "FARMING INTELLIGENCE   /   MATCH ANALYTICS   /   EXPERIENCE SCREENING"
            ),
            font=("Segoe UI", 10),
            bg=self.BG,
            fg=self.MUTED,
        ).pack(anchor="w", pady=(3, 0))

        lookup = tk.Frame(
            self,
            bg=self.PANEL,
            padx=12,
            pady=12,
        )
        lookup.pack(fill="x", padx=18, pady=(4, 12))

        tk.Label(
            lookup,
            text="Steam profile URL or SteamID64",
            bg=self.PANEL,
            fg=self.TEXT,
        ).pack(side="left", padx=(0, 10))

        self.profile_entry = ttk.Entry(lookup)
        self.profile_entry.pack(
            side="left",
            fill="x",
            expand=True,
            padx=(0, 10),
        )
        self.profile_entry.bind(
            "<Return>",
            lambda _event: self.start_lookup(),
        )

        self.lookup_button = ttk.Button(
            lookup,
            text="Analyze Profile",
            command=self.start_lookup,
        )
        self.lookup_button.pack(side="left")

        self.status_label = tk.Label(
            self,
            text="Ready",
            bg=self.BG,
            fg=self.MUTED,
            anchor="w",
        )
        self.status_label.pack(
            fill="x",
            padx=20,
            pady=(0, 8),
        )

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(
            fill="both",
            expand=True,
            padx=18,
            pady=(0, 18),
        )

        self.dashboard_tab = ttk.Frame(self.notebook)
        self.report_tab = ttk.Frame(self.notebook)
        self.match_tab = ttk.Frame(self.notebook)
        self.benchmark_tab = ttk.Frame(self.notebook)
        self.console_tab = ttk.Frame(self.notebook)

        self.notebook.add(
            self.dashboard_tab,
            text="Profile Dashboard",
        )
        self.notebook.add(
            self.match_tab,
            text="Match Lookup",
        )
        self.notebook.add(
            self.report_tab,
            text="Raw Report",
        )
        self.notebook.add(self.benchmark_tab, text="Rank Benchmarks")
        self.notebook.add(self.console_tab, text="Console")

        self.build_dashboard()
        self.build_report_tab()
        self.build_match_tab()
        self.build_benchmark_tab()
        self.build_console_tab()
        self.install_dashboard_tooltips()

    # --------------------------------------------------------
    # Dashboard
    # --------------------------------------------------------

    def build_dashboard(self):
        outer = tk.Frame(self.dashboard_tab, bg=self.BG)
        outer.pack(fill="both", expand=True)

        self.dashboard_canvas = tk.Canvas(
            outer,
            bg=self.BG,
            highlightthickness=0,
        )

        scrollbar = ttk.Scrollbar(
            outer,
            orient="vertical",
            command=self.dashboard_canvas.yview,
        )

        self.dashboard_canvas.configure(
            yscrollcommand=scrollbar.set,
        )

        scrollbar.pack(side="right", fill="y")
        self.dashboard_canvas.pack(
            side="left",
            fill="both",
            expand=True,
        )

        self.dashboard_content = tk.Frame(
            self.dashboard_canvas,
            bg=self.BG,
        )

        self.dashboard_window = self.dashboard_canvas.create_window(
            (0, 0),
            window=self.dashboard_content,
            anchor="nw",
        )

        self.dashboard_content.bind(
            "<Configure>",
            lambda _event: self.dashboard_canvas.configure(
                scrollregion=self.dashboard_canvas.bbox("all")
            ),
        )

        self.dashboard_canvas.bind(
            "<Configure>",
            lambda event: self.dashboard_canvas.itemconfigure(
                self.dashboard_window,
                width=event.width,
            ),
        )

        # Risk and score panel.
        self.score_panel = tk.Frame(
            self.dashboard_content,
            bg=self.PANEL,
            padx=18,
            pady=16,
        )
        self.score_panel.pack(
            fill="x",
            padx=8,
            pady=8,
        )

        self.player_name_label = tk.Label(
            self.score_panel,
            text="No profile analyzed",
            font=("Segoe UI", 16, "bold"),
            bg=self.PANEL,
            fg=self.TEXT,
        )
        self.player_name_label.pack(anchor="w")

        self.score_label = tk.Label(
            self.score_panel,
            text="Smurf screening score: —",
            font=("Segoe UI", 12, "bold"),
            bg=self.PANEL,
            fg=self.TEXT,
        )
        self.score_label.pack(anchor="w", pady=(8, 6))

        self.score_canvas = tk.Canvas(
            self.score_panel,
            height=16,
            bg=self.PANEL_ALT,
            highlightthickness=0,
        )
        self.score_canvas.pack(fill="x", pady=(2, 8))

        # Separate prominent risk badge.
        self.risk_label = tk.Label(
            self.score_panel,
            text="AWAITING ANALYSIS",
            font=("Segoe UI", 10, "bold"),
            bg=self.PANEL_ALT,
            fg=self.MUTED,
            padx=10,
            pady=6,
        )
        self.risk_label.pack(anchor="w")

        self.confidence_label = tk.Label(
            self.score_panel, text="Evidence confidence: —  ⓘ  (hover for explanation)",
            font=("Segoe UI", 10, "bold"), bg=self.PANEL, fg=self.ACCENT,
            anchor="w", padx=0, pady=6,
        )
        self.confidence_label.pack(anchor="w", pady=(3, 0))

        self.screening_details_label = tk.Label(
            self.score_panel,
            text="Experience and evidence breakdown appears after analysis.",
            font=("Segoe UI", 9), bg=self.PANEL, fg=self.MUTED,
            anchor="w", justify="left", wraplength=900,
        )
        self.screening_details_label.pack(anchor="w", pady=(10, 0))

        # Summary metrics.
        self.metric_frame = tk.Frame(
            self.dashboard_content,
            bg=self.BG,
        )
        self.metric_frame.pack(fill="x", padx=4)

        self.metric_labels = {}

        metrics = [
            ("Win Rate", "win_rate"),
            ("Average KDA", "average_kda"),
            ("Recent KDA", "recent_kda"),
            ("Matches Returned", "match_count"),
        ]

        for index, (label, key) in enumerate(metrics):
            card = tk.Frame(
                self.metric_frame,
                bg=self.PANEL,
                padx=12,
                pady=12,
            )

            card.grid(
                row=0,
                column=index,
                sticky="nsew",
                padx=4,
                pady=4,
            )

            self.metric_frame.columnconfigure(
                index,
                weight=1,
            )

            tk.Label(
                card,
                text=label,
                bg=self.PANEL,
                fg=self.MUTED,
            ).pack(anchor="w")

            value_label = tk.Label(
                card,
                text="—",
                bg=self.PANEL,
                fg=self.TEXT,
                font=("Segoe UI", 18, "bold"),
            )
            value_label.pack(anchor="w", pady=(5, 0))

            self.metric_labels[key] = value_label

        # Accuracy and critical-shot panels.
        self.aim_frame = tk.Frame(
            self.dashboard_content,
            bg=self.BG,
        )
        self.aim_frame.pack(fill="x", padx=4)

        self.accuracy_label = self.make_rate_panel(
            self.aim_frame,
            "Accuracy",
            0,
        )

        self.crit_label = self.make_rate_panel(
            self.aim_frame,
            "Critical Shot Rate",
            1,
        )

        # Farming dashboard: comparison cards, difference badge, and visual bars.
        self.farming_panel = tk.Frame(self.dashboard_content, bg=self.PANEL, padx=16, pady=14)
        self.farming_panel.pack(fill="x", padx=8, pady=8)
        tk.Label(self.farming_panel, text="Farming & Soul Economy",
                 font=("Segoe UI", 13, "bold"), bg=self.PANEL, fg=self.TEXT).pack(anchor="w")
        tk.Label(self.farming_panel, text="Profile farming consistency compared with players in recent shared matches",
                 font=("Segoe UI", 9), bg=self.PANEL, fg=self.MUTED).pack(anchor="w", pady=(3, 12))

        comparison = tk.Frame(self.farming_panel, bg=self.PANEL)
        comparison.pack(fill="x")
        comparison.columnconfigure(0, weight=1, uniform="farm")
        comparison.columnconfigure(1, weight=1, uniform="farm")
        comparison.columnconfigure(2, weight=1, uniform="farm")
        self.farming_metrics = {}
        for index, (key, heading) in enumerate((
            ("player", "PLAYER MEDIAN SOULS / MIN"),
            ("peer", "PEER MEDIAN SOULS / MIN"),
            ("difference", "DIFFERENCE VS PEERS"),
        )):
            card = tk.Frame(comparison, bg=self.PANEL_ALT, padx=12, pady=12)
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 5, 0))
            tk.Label(card, text=heading, bg=self.PANEL_ALT, fg=self.MUTED,
                     font=("Segoe UI", 9, "bold")).pack(anchor="w")
            value = tk.Label(card, text="—", bg=self.PANEL_ALT, fg=self.TEXT,
                             font=("Segoe UI", 21, "bold"))
            value.pack(anchor="w", pady=(5, 0))
            self.farming_metrics[key] = value

        self.farming_compare_status = tk.Label(
            self.farming_panel, text="Analyze a profile to compare farming.",
            bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 9), anchor="w")
        self.farming_compare_status.pack(fill="x", pady=(12, 6))

        self.farming_compare_canvas = tk.Canvas(
            self.farming_panel, bg=self.PANEL, highlightthickness=0, height=65)
        self.farming_compare_canvas.pack(fill="x", pady=(0, 9))
        self.farming_compare_canvas.bind("<Configure>", lambda _event: self.draw_farming_bars())
        self.farming_bar_values = (None, None)

        self.farming_extra = tk.Frame(self.farming_panel, bg=self.PANEL)
        self.farming_extra.pack(fill="x", pady=(4, 0))
        self.farming_extra.columnconfigure(0, weight=1, uniform="farmextra")
        self.farming_extra.columnconfigure(1, weight=1, uniform="farmextra")
        self.farming_extra.columnconfigure(2, weight=1, uniform="farmextra")
        self.farming_extra_labels = {}
        for index, (key, title) in enumerate((
            ("last_hits", "AVG LAST HITS"),
            ("denies", "AVG DENIES"),
            ("consistency", "HIGH SOUL-RATE GAMES"),
        )):
            item = tk.Frame(self.farming_extra, bg=self.PANEL)
            item.grid(row=0, column=index, sticky="ew", padx=(0, 8))
            tk.Label(item, text=title, bg=self.PANEL, fg=self.MUTED,
                     font=("Segoe UI", 8, "bold")).pack(anchor="w")
            label = tk.Label(item, text="—", bg=self.PANEL, fg=self.TEXT,
                             font=("Segoe UI", 15, "bold"))
            label.pack(anchor="w", pady=(2, 0))
            self.farming_extra_labels[key] = label

        self.farming_details_label = tk.Label(
            self.farming_panel, text="", bg=self.PANEL, fg=self.MUTED,
            font=("Segoe UI", 9), justify="left", anchor="w", wraplength=1000)
        self.farming_details_label.pack(fill="x", pady=(12, 0))

        # Top heroes.
        self.hero_panel = tk.Frame(
            self.dashboard_content,
            bg=self.PANEL,
            padx=14,
            pady=12,
        )
        self.hero_panel.pack(
            fill="x",
            padx=8,
            pady=8,
        )

        tk.Label(
            self.hero_panel,
            text="Top Heroes",
            font=("Segoe UI", 12, "bold"),
            bg=self.PANEL,
            fg=self.TEXT,
        ).pack(anchor="w")

        self.hero_text = tk.Label(
            self.hero_panel,
            text="Analyze a profile to view hero usage.",
            bg=self.PANEL,
            fg=self.MUTED,
            justify="left",
            anchor="w",
        )
        self.hero_text.pack(
            fill="x",
            pady=(8, 0),
        )

        # Suspicion indicators are factual observations, not data-coverage badges.
        self.indicators_panel = tk.Frame(self.dashboard_content, bg=self.PANEL, padx=14, pady=12)
        self.indicators_panel.pack(fill="x", padx=8, pady=8)
        tk.Label(self.indicators_panel, text="Screening Indicators",
                 font=("Segoe UI", 12, "bold"), bg=self.PANEL, fg=self.TEXT).pack(anchor="w", pady=(0, 6))
        self.indicators_text = tk.Text(
            self.indicators_panel, height=7, wrap="word", bg=self.PANEL_ALT,
            fg=self.TEXT, insertbackground=self.TEXT, relief="flat",
            font=("Segoe UI", 10), padx=12, pady=10,
        )
        self.indicators_text.pack(fill="x")
        self.indicators_text.insert("1.0", "Analyze a profile to view potential suspicion datapoints.")
        self.indicators_text.configure(state="disabled")

        self.note_label = tk.Label(
            self.dashboard_content,
            text=(
                "Screening scores are heuristic indicators, not proof of "
                "cheating or smurfing. Missing data is shown as N/A."
            ),
            bg=self.BG,
            fg=self.MUTED,
            wraplength=950,
            justify="left",
        )
        self.note_label.pack(
            fill="x",
            padx=12,
            pady=12,
        )

    def install_dashboard_tooltips(self):
        self.add_help(self.score_label, "score")
        self.add_help(self.confidence_label, content=lambda: self.confidence_explanation(self.current_analysis))
        self.add_help(self.risk_label, content=lambda: self.help_text("risk") + "\n\n" + self.confidence_explanation(self.current_analysis))
        self.add_help(self.screening_details_label, "screening_details")
        self.add_help(self.accuracy_label, "accuracy")
        self.add_help(self.crit_label, "crit")
        self.add_help(self.farming_details_label, "farming_details")
        self.add_help(self.indicators_text, "indicators")
        for key, widget in self.metric_labels.items():
            self.add_help(widget, key)
        for key, widget in self.farming_metrics.items():
            self.add_help(widget, key)
        for key, widget in self.farming_extra_labels.items():
            self.add_help(widget, key)

    def make_rate_panel(self, parent, title, column):
        panel = tk.Frame(
            parent,
            bg=self.PANEL,
            padx=14,
            pady=12,
        )

        panel.grid(
            row=0,
            column=column,
            sticky="nsew",
            padx=4,
            pady=4,
        )

        parent.columnconfigure(column, weight=1)

        tk.Label(
            panel,
            text=title,
            bg=self.PANEL,
            fg=self.MUTED,
        ).pack(anchor="w")

        value = tk.Label(
            panel,
            text="N/A",
            bg=self.PANEL,
            fg=self.TEXT,
            font=("Segoe UI", 18, "bold"),
        )
        value.pack(anchor="w", pady=(5, 8))

        canvas = tk.Canvas(
            panel,
            height=12,
            bg=self.PANEL_ALT,
            highlightthickness=0,
        )
        canvas.pack(fill="x")

        if title == "Accuracy":
            self.accuracy_bar = canvas
        else:
            self.crit_bar = canvas

        return value

    # --------------------------------------------------------
    # Raw report tab
    # --------------------------------------------------------

    def build_report_tab(self):
        toolbar = ttk.Frame(self.report_tab)
        toolbar.pack(
            fill="x",
            padx=8,
            pady=8,
        )

        ttk.Button(
            toolbar,
            text="Copy Report",
            command=self.copy_report,
        ).pack(side="left")

        ttk.Button(
            toolbar,
            text="Copy Smurf Report Description",
            command=self.copy_smurf_report_description,
        ).pack(side="left", padx=(8, 0))

        ttk.Button(
            toolbar,
            text="Refresh Profile",
            command=self.start_lookup,
        ).pack(side="left", padx=8)

        # The text and scrollbar must share the same containing frame.
        # This avoids geometry-management inconsistencies when changing tabs.
        report_holder = tk.Frame(self.report_tab, bg=self.BG)
        report_holder.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.report_text = tk.Text(
            report_holder,
            wrap="word",
            bg=self.PANEL,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            relief="flat",
            padx=12,
            pady=12,
            font=("Consolas", 10),
        )

        report_scroll = ttk.Scrollbar(report_holder, orient="vertical", command=self.report_text.yview)
        self.report_text.configure(yscrollcommand=report_scroll.set)
        report_scroll.pack(side="right", fill="y")
        self.report_text.pack(side="left", fill="both", expand=True)
        # Mouse wheel is handled natively by the Text widget on Windows.
        self.report_text.bind("<MouseWheel>", self._scroll_report)
        self.report_text.bind("<Button-4>", self._scroll_report)
        self.report_text.bind("<Button-5>", self._scroll_report)

        self.report_text.insert(
            "1.0",
            "Analyze a profile to generate a report.",
        )
        self.report_text.configure(state="disabled")

    # --------------------------------------------------------
    # Match lookup tab
    # --------------------------------------------------------

    def build_match_tab(self):
        top = ttk.Frame(self.match_tab)
        top.pack(
            fill="x",
            padx=8,
            pady=8,
        )

        ttk.Label(
            top,
            text="Match ID",
        ).pack(side="left", padx=(0, 8))

        self.match_id_entry = ttk.Entry(top)
        self.match_id_entry.pack(
            side="left",
            fill="x",
            expand=True,
            padx=(0, 8),
        )

        self.match_id_entry.bind(
            "<Return>",
            lambda _event: self.start_match_lookup(),
        )

        ttk.Button(
            top,
            text="Look Up Match",
            command=self.start_match_lookup,
        ).pack(side="left")

        ttk.Button(
            top,
            text="Copy Match JSON",
            command=self.copy_match_json,
        ).pack(side="left", padx=(8, 0))

        self.match_status = tk.Entry(
            self.match_tab, readonlybackground=self.BG, fg=self.MUTED,
            relief="flat", bd=0, highlightthickness=0,
            font=("Segoe UI", 10), exportselection=False,
        )
        self.set_match_status("Enter a match ID to inspect its metadata.")
        self.match_status.pack(fill="x", padx=10, pady=(0, 8))

        # Scrollable match area: the two team columns resize to the tab width,
        # and the vertical scrollbar is available when the window is too short.
        match_area = tk.Frame(self.match_tab, bg=self.BG)
        match_area.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.match_canvas = tk.Canvas(
            match_area, bg=self.BG, highlightthickness=0, borderwidth=0
        )
        self.match_scrollbar = ttk.Scrollbar(
            match_area, orient="vertical", command=self.match_canvas.yview
        )
        self.match_canvas.configure(yscrollcommand=self.match_scrollbar.set)
        self.match_scrollbar.pack(side="right", fill="y")
        self.match_canvas.pack(side="left", fill="both", expand=True)

        self.teams_frame = tk.Frame(self.match_canvas, bg=self.BG)
        self.teams_window = self.match_canvas.create_window(
            (0, 0), window=self.teams_frame, anchor="nw"
        )
        self.teams_frame.bind(
            "<Configure>",
            lambda _event: self.match_canvas.configure(
                scrollregion=self.match_canvas.bbox("all")
            ),
        )
        self.match_canvas.bind(
            "<Configure>",
            lambda event: self.match_canvas.itemconfigure(
                self.teams_window, width=event.width
            ),
        )
        # A single wheel dispatcher supports both scrollable tabs, including
        # labels and cards nested inside their canvases.
        self.bind_all("<MouseWheel>", self._scroll_active_panel)
        self.bind_all("<Button-4>", self._scroll_active_panel)
        self.bind_all("<Button-5>", self._scroll_active_panel)

        self.teams_frame.grid_columnconfigure(0, weight=1, uniform="team")
        self.teams_frame.grid_columnconfigure(1, weight=1, uniform="team")
        self.team_frames = []

        for index, title in enumerate(("Team 1", "Team 2")):
            frame = tk.Frame(
                self.teams_frame, bg=self.PANEL, padx=7, pady=7
            )
            frame.grid(row=0, column=index, sticky="nsew", padx=4, pady=3)

            tk.Label(
                frame, text=title, bg=self.PANEL, fg=self.TEXT,
                font=("Segoe UI", 11, "bold"),
            ).pack(anchor="w", pady=(0, 4))
            self.team_frames.append(frame)

    def _scroll_active_panel(self, event):
        """Route wheel events to the scrollable panel under the pointer."""
        widget = self.winfo_containing(event.x_root, event.y_root)
        current = widget
        target = None
        while current is not None:
            if current in (self.dashboard_canvas, self.dashboard_content):
                target = self.dashboard_canvas
                break
            if current in (self.match_canvas, self.teams_frame):
                target = self.match_canvas
                break
            current = getattr(current, "master", None)

        if target is None:
            return  # Let other controls (such as the report text) scroll normally.

        if getattr(event, "num", None) in (4, 5):
            steps = -1 if event.num == 4 else 1
        else:
            delta = getattr(event, "delta", 0)
            if not delta:
                return
            steps = -max(1, abs(delta) // 120) if delta > 0 else max(1, abs(delta) // 120)
        target.yview_scroll(steps, "units")
        return "break"


    # --------------------------------------------------------
    # Rank benchmarks — independent of the smurf score
    # --------------------------------------------------------

    def build_benchmark_tab(self):
        parent = tk.Frame(self.benchmark_tab, bg=self.BG, padx=15, pady=14)
        parent.pack(fill="both", expand=True)
        tk.Label(parent, text="Performance Benchmarks  /  General + Optional Rank", bg=self.BG,
                 fg=self.TEXT, font=("Segoe UI", 17, "bold")).pack(anchor="w")
        tk.Label(parent, text=(
            "Optional 500-match target per historical lobby-rank tier | Normal mode | Ranked + unranked | "
            "Last 30 days\n"
            "Averages and percentiles describe individual player-match rows, not "
            "career-average player rankings."
        ), bg=self.BG, fg=self.MUTED, justify="left", wraplength=1050,
                 font=("Segoe UI", 10)).pack(anchor="w", pady=(5, 12))
        general = tk.Frame(parent, bg=self.PANEL, padx=12, pady=10)
        general.pack(fill="x", pady=(0, 12))
        ttk.Button(general, text="Import general benchmark JSON", command=self.import_general_benchmark).pack(side="left")
        ttk.Button(general, text="Fetch general benchmark", command=self.start_general_benchmark_fetch).pack(side="left", padx=8)
        self.general_benchmark_status = tk.Label(general, text="", bg=self.PANEL, fg=self.MUTED,
                                                  justify="left", anchor="w", wraplength=530)
        self.general_benchmark_status.pack(side="left", fill="x", expand=True, padx=10)
        self.refresh_general_benchmark_status()
        toolbar = tk.Frame(parent, bg=self.BG)
        toolbar.pack(fill="x", pady=(0, 10))
        self.benchmark_button = ttk.Button(toolbar, text="Fetch OPTIONAL rank benchmarks",
                                           command=self.start_benchmark_lookup)
        self.benchmark_button.pack(side="left")
        self.benchmark_status = tk.Label(toolbar, text="Not loaded — click Fetch rank benchmarks",
                                         bg=self.BG, fg=self.MUTED, anchor="w")
        self.benchmark_status.pack(side="left", padx=14)
        columns = ("rank", "sample", "souls", "hits", "denies", "kda", "accuracy", "crit")
        frame = tk.Frame(parent, bg=self.BG)
        frame.pack(fill="both", expand=True)
        self.benchmark_tree = ttk.Treeview(frame, columns=columns, show="headings", height=14)
        headers = ("Lobby rank", "Matches*", "Souls/min", "LH/min", "Denies/min",
                   "KDA", "Accuracy", "Crit rate")
        for col, title in zip(columns, headers):
            self.benchmark_tree.heading(col, text=title)
            self.benchmark_tree.column(col, width=145 if col == "rank" else 104,
                                       anchor="w" if col == "rank" else "center", minwidth=78)
        vs = ttk.Scrollbar(frame, orient="vertical", command=self.benchmark_tree.yview)
        hs = ttk.Scrollbar(frame, orient="horizontal", command=self.benchmark_tree.xview)
        self.benchmark_tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.benchmark_tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        self.benchmark_tree.bind("<<TreeviewSelect>>", self.show_benchmark_detail)
        self.benchmark_detail = tk.Text(parent, height=9, bg=self.PANEL, fg=self.TEXT,
                                         insertbackground=self.TEXT, relief="flat", wrap="word",
                                         font=("Consolas", 10), padx=12, pady=10)
        self.benchmark_detail.pack(fill="x", pady=(12, 0))
        self.benchmark_detail.insert("1.0", "Select a rank to view its average, median, P75, P90, and coverage.")
        self.benchmark_detail.configure(state="disabled")
        self.benchmark_tree.tag_configure("failed", foreground=self.RED)
        self.benchmark_tree.tag_configure("loaded", foreground=self.GREEN)

    def refresh_general_benchmark_status(self):
        record = load_general_benchmark()
        if record:
            node = record["metrics"]["net_worth_per_min"]
            self.general_benchmark_status.configure(
                text=f"Loaded • {record['source']} • {record['retrieved_at']}\n"
                     f"Souls/min P50 {node['percentile50']:,.0f}  /  P90 {node['percentile90']:,.0f}  /  P95 {node['percentile95']:,.0f}",
                fg=self.GREEN)
        else:
            self.general_benchmark_status.configure(text="No general benchmark loaded. Import response.json or fetch from API.", fg=self.YELLOW)

    def import_general_benchmark(self):
        path = filedialog.askopenfilename(title="Import player-stats/metrics response", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            # Also accept an exported cache record, preserving source context.
            if "metrics" in payload and "source" in payload:
                payload = payload["metrics"]
            save_general_benchmark(payload, source=f"Imported {Path(path).name} (query filters unknown)")
            self.refresh_general_benchmark_status()
            messagebox.showinfo("Benchmark imported", "Benchmark saved. Reanalyze a profile to apply it.\nImported request parameters cannot be independently verified.")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            messagebox.showerror("Invalid benchmark JSON", str(exc))

    def start_general_benchmark_fetch(self):
        if self.general_benchmark_loading:
            return
        self.general_benchmark_loading = True
        cached = load_general_benchmark()
        if cached:
            status = "Cached benchmark available for scoring • refreshing from API in background..."
        else:
            status = "Fetching general benchmark for scoring in background..."
        self.general_benchmark_status.configure(text=status, fg=self.YELLOW)
        threading.Thread(target=self.general_benchmark_worker, daemon=True).start()

    def general_benchmark_worker(self):
        params = {"max_matches": 500, "game_mode": "normal", "match_mode": "ranked,unranked"}
        try:
            data = api_get(BENCHMARK_URL_PATH, params)
            save_general_benchmark(data, source="Deadlock API (unfiltered by rank)", params=params)
            self.after(0, self.general_benchmark_fetch_finished, None)
        except Exception as exc:
            self.after(0, self.general_benchmark_fetch_finished, str(exc))

    def general_benchmark_fetch_finished(self, error):
        self.general_benchmark_loading = False
        if error is None:
            self.refresh_general_benchmark_status()
        elif load_general_benchmark():
            self.general_benchmark_status.configure(
                text=f"API refresh failed: {error}\nUsing cached benchmark for scoring.", fg=self.YELLOW)
        else:
            self.general_benchmark_status.configure(
                text=f"Benchmark unavailable: {error}\nScoring will use available non-benchmark evidence.", fg=self.RED)

    def start_benchmark_lookup(self):
        if self.rank_benchmark_loading:
            return
        self.rank_benchmark_loading = True
        self.rank_benchmark_rows = {}
        self.benchmark_button.configure(state="disabled")
        self.benchmark_status.configure(text="Loading rank tiers sequentially...", fg=self.YELLOW)
        for item in self.benchmark_tree.get_children():
            self.benchmark_tree.delete(item)
        threading.Thread(target=self.benchmark_worker, daemon=True).start()

    def benchmark_worker(self):
        try:
            ranks = benchmark_rank_rows()
            for idx, (tier, name) in enumerate(ranks):
                try:
                    payload = fetch_rank_benchmark(tier)
                    error = None
                except Exception as exc:
                    payload = None
                    error = str(exc)
                self.after(0, lambda t=tier, n=name, p=payload, e=error, i=idx:
                           self.append_benchmark_row(t, n, p, e, i))
        finally:
            self.after(0, self.finish_benchmark_lookup)

    def append_benchmark_row(self, tier, name, payload, error, idx):
        self.rank_benchmark_rows[tier] = (name, payload, error)
        if error:
            values = (name, "Error", "—", "—", "—", "—", "—", "—")
        else:
            count = benchmark_sample_count(payload)
            metrics = []
            for label, keys in RANK_BENCHMARK_FIELDS:
                node = benchmark_metric_node(payload, keys)
                value = benchmark_stat_value(node)
                if value is None:
                    metrics.append("—")
                elif label in ("Accuracy", "Crit rate"):
                    # Percentage representation is not inferred from the magnitude.
                    metrics.append(f"{value:.2f} (raw)")
                else:
                    metrics.append(f"{value:,.1f}")
            values = (name, str(count) if count is not None else "N/A", *metrics)
        self.benchmark_tree.insert("", "end", iid=str(tier), values=values,
                                   tags=("failed" if error else "loaded",))
        self.benchmark_status.configure(text=f"Loaded {idx+1}/11 rank tiers...", fg=self.YELLOW)

    def finish_benchmark_lookup(self):
        self.rank_benchmark_loading = False
        self.benchmark_button.configure(state="normal")
        failed = sum(bool(row[2]) for row in self.rank_benchmark_rows.values())
        self.benchmark_status.configure(
            text=f"Finished: {len(self.rank_benchmark_rows)} tiers, {failed} errors. Click a row for details.",
            fg=self.YELLOW if failed else self.GREEN)

    def show_benchmark_detail(self, _event=None):
        selection = self.benchmark_tree.selection()
        if not selection:
            return
        tier = int(selection[0])
        name, data, error = self.rank_benchmark_rows.get(tier, ("Unknown", None, "Missing data"))
        lines = [f"{name} — average lobby badge {tier*10} to {tier*10+6}",
                 "Target: up to 500 matches | Ranked + unranked | Last 30 days", ""]
        if error:
            lines.append("API error: " + error)
        else:
            count = benchmark_sample_count(data)
            lines.append("Reported match sample: " + (str(count) if count is not None else
                         "Unavailable (API did not expose a verified count)"))
            lines.append("Percentiles are across single player-match observations.\n")
            for label, keys in RANK_BENCHMARK_FIELDS:
                node = benchmark_metric_node(data, keys)
                if node is None:
                    lines.append(f"{label}: unavailable (no matching API metric)")
                    continue
                values = [benchmark_stat_value(node, kind) for kind in ("avg", "p50", "p75", "p90")]
                parts = [f"{key}={value:.3f}" if value is not None else f"{key}=N/A"
                         for key, value in zip(("mean", "median", "P75", "P90"), values)]
                lines.append(f"{label}: " + " | ".join(parts))
            lines.append("Available API metric keys: " + ", ".join(sorted(str(k) for k in (data.get("metrics") if isinstance(data.get("metrics"), dict) else data).keys())[:80]))
            lines.extend(("", "Accuracy and crit-rate values are shown RAW because the API's",
                          "unit conventions must be confirmed against a live response.",
                          "Benchmarks do not modify the profile's smurf screening score."))
        self.benchmark_detail.configure(state="normal")
        self.benchmark_detail.delete("1.0", "end")
        self.benchmark_detail.insert("1.0", "\n".join(lines))
        self.benchmark_detail.configure(state="disabled")

    # --------------------------------------------------------
    # Profile lookup
    # --------------------------------------------------------

    def start_lookup(self):
        value = self.profile_entry.get().strip()

        if not value:
            messagebox.showwarning(
                "Missing profile",
                "Enter a Steam profile URL or SteamID64.",
            )
            return

        self.lookup_button.configure(state="disabled")

        self.status_label.configure(
            text="Resolving Steam profile and retrieving statistics…",
            fg=self.YELLOW,
        )

        threading.Thread(
            target=self.profile_worker,
            args=(value,),
            daemon=True,
        ).start()

    def profile_worker(self, value):
        try:
            steam64 = steam_id_from_input(value)

            if steam64 is None:
                account_id = int(value)
            else:
                account_id = steam64_to_account_id(steam64)

            player = get_player(account_id)
            rank = get_rank(account_id)
            history = get_history(account_id)
            hero_data = get_player_hero_stats(account_id)
            hero_names = get_hero_names()

            analysis = analyze_history(history)
            try:
                analysis["peer_comparison"] = compare_farming_to_peers(history, account_id)
            except Exception:
                analysis["peer_comparison"] = None
            calculate_screening_v50(analysis, player, rank)
            stats = aggregate_hero_stats(hero_data)
            top_heroes = get_top_heroes(
                hero_data,
                hero_names,
            )

            report = build_report(
                account_id,
                player,
                rank,
                analysis,
                stats,
                top_heroes,
            )

            result = {
                "account_id": account_id,
                "player": player,
                "rank": rank,
                "analysis": analysis,
                "stats": stats,
                "top_heroes": top_heroes,
                "report": report,
                "hero_names": hero_names,
            }

            self.after(
                0,
                lambda: self.profile_success(result),
            )

        except Exception as exc:
            error = str(exc)

            self.after(
                0,
                lambda: self.profile_failure(error),
            )

    def profile_success(self, result):
        self.lookup_button.configure(state="normal")

        self.current_account_id = result["account_id"]
        self.current_player = result["player"]
        self.current_rank = result["rank"]
        self.current_analysis = result["analysis"]
        self.current_stats = result["stats"]
        self.current_top_heroes = result["top_heroes"]
        self.current_report = result["report"]
        self.hero_names = result["hero_names"]

        self.status_label.configure(
            text=f"Analysis complete · Account ID {self.current_account_id}",
            fg=self.GREEN,
        )

        # Populate the report first so dashboard rendering cannot leave it blank.
        self.set_report(self.current_report)
        self.update_dashboard()
        # A profile opened from Match Lookup should reveal its dashboard.
        self.notebook.select(self.dashboard_tab)

    def profile_failure(self, error):
        self.lookup_button.configure(state="normal")

        self.status_label.configure(
            text="Profile analysis failed.",
            fg=self.RED,
        )

        messagebox.showerror(
            "Analysis error",
            error,
        )

    # --------------------------------------------------------
    # Dashboard updates
    # --------------------------------------------------------

    def draw_farming_bars(self):
        """Render side-by-side economy bars, scaled to the two observed medians."""
        canvas = self.farming_compare_canvas
        canvas.delete("all")
        width = canvas.winfo_width()
        if width < 100:
            return
        own, peer = self.farming_bar_values
        if own is None and peer is None:
            canvas.create_text(8, 28, anchor="w", text="Comparison appears when peer data is available",
                               fill=self.MUTED, font=("Segoe UI", 9))
            return
        left, right = 120, max(140, width - 12)
        track_width = max(1, right - left)
        maximum = max(1, own or 0, peer or 0)
        for index, (label, value, color) in enumerate((
            ("Player median", own, self.ACCENT),
            ("Peer median", peer, self.GREEN),
        )):
            y = 7 + index * 30
            canvas.create_text(4, y + 8, text=label, anchor="w", fill=self.MUTED,
                               font=("Segoe UI", 9))
            canvas.create_rectangle(left, y, right, y + 17, fill=self.PANEL_ALT, outline="")
            if value is not None:
                filled = track_width * max(0, value) / maximum
                canvas.create_rectangle(left, y, left + filled, y + 17, fill=color, outline="")
                canvas.create_text(right - 6, y + 8, anchor="e", text=fmt_number(value, 0),
                                   fill=self.TEXT, font=("Segoe UI", 9, "bold"))

    def update_dashboard(self):
        analysis = self.current_analysis
        stats = self.current_stats
        player = self.current_player

        if not analysis:
            return

        name = first_value(
            player,
            (
                "personaname",
                "persona_name",
                "name",
                "player_name",
            ),
        ) or f"Account {self.current_account_id}"

        self.player_name_label.configure(text=str(name))

        self.score_label.configure(
            text=(
                f"Smurf screening score: "
                f"{analysis['score']}/100"
            )
        )

        # Risk badge.
        style = self.RISK_STYLES.get(
            analysis["risk"],
            {
                "label": "RISK UNKNOWN",
                "background": self.PANEL_ALT,
                "foreground": self.MUTED,
            },
        )

        self.risk_label.configure(
            text=(
                f"{style['label']}   |   "
                f"Confidence: {analysis['confidence']}   |   "
                f"Model: {analysis.get('score_version', '0.5.8')}"
            ),
            bg=style["background"],
            fg=style["foreground"],
        )

        self.confidence_label.configure(text=f"Evidence confidence: {analysis.get('confidence', 'Unknown')}   ⓘ  (hover for reasons)")

        lifetime = analysis.get("lifetime_matches")
        experience_text = (f"{lifetime:,} lifetime matches (API reported)" if lifetime is not None
                           else "Lifetime matches unavailable; returned history is not lifetime total")
        farming_component = analysis.get("farming_component")
        farm_text = (f"{farming_component:.1f}/100" if farming_component is not None
                     else "Unavailable (combat-only score)")
        self.screening_details_label.configure(text=(
            f"Combat anomaly: {fmt_number(analysis.get('combat_component'))}/100  •  "
            f"Farming anomaly: {farm_text}\n"
            f"Experience: {experience_text}  •  "
            f"Returned: {analysis['match_count']} matches\n"
            f"Benchmark-matched games: {analysis.get('benchmark_coverage', 0)}  •  "
            f"Experience mismatch: {fmt_number(analysis.get('experience_component'))}/100  •  "
            f"Valid KDA/results: {analysis['valid_kda_count']}/{analysis['valid_result_count']}\n"
            f"Experience context: {analysis.get('experience_label', 'Unavailable')}\n"
            f"Farming games above benchmark P90: {analysis.get('farming', {}).get('benchmark_p90_matches', 0)} / {analysis.get('benchmark_coverage', 0)}\n"
            f"Learning trend: {analysis.get('learning_progression', {}).get('status', 'Unavailable')}"
        ))

        # Summary metrics.
        self.metric_labels["win_rate"].configure(
            text=fmt_percent(analysis["win_rate"])
        )

        self.metric_labels["average_kda"].configure(
            text=fmt_number(analysis["average_kda"])
        )

        self.metric_labels["recent_kda"].configure(
            text=fmt_number(analysis["recent_kda"])
        )

        self.metric_labels["match_count"].configure(
            text=str(analysis["match_count"])
        )

        # Aim metrics and bars.
        self.accuracy_label.configure(
            text=fmt_percent(stats["accuracy"])
        )

        self.crit_label.configure(
            text=fmt_percent(stats["crit_rate"])
        )

        self.after(
            100,
            lambda: self.draw_rate_bar(
                self.accuracy_bar,
                stats["accuracy"],
                self.ACCENT,
            ),
        )

        self.after(
            100,
            lambda: self.draw_rate_bar(
                self.crit_bar,
                stats["crit_rate"],
                self.GREEN,
            ),
        )

        # Score bar.
        self.after(100, self.redraw_score_bar)

        # Farming: visual comparison, sample size, and underlying match averages.
        farming = analysis.get("farming", {})
        peer = analysis.get("peer_comparison") or {}
        own = peer.get("player_median")
        if own is None:
            own = farming.get("souls_per_min_median")
        benchmark = peer.get("peer_median")
        difference = peer.get("difference_pct")
        self.farming_metrics["player"].configure(text=fmt_number(own, 0))
        self.farming_metrics["peer"].configure(text=fmt_number(benchmark, 0))
        if difference is None:
            self.farming_metrics["difference"].configure(text="N/A", fg=self.MUTED)
        else:
            # Higher economy is not by itself evidence of smurfing.
            self.farming_metrics["difference"].configure(
                text=f"{difference:+.1f}%",
                fg=self.GREEN if difference >= 0 else self.YELLOW)
        self.farming_bar_values = (own, benchmark)
        self.after(50, self.draw_farming_bars)
        observations = peer.get("peer_observations", 0)
        matches = peer.get("peer_matches", 0)
        if difference is None:
            status = f"Peer benchmark unavailable: {peer.get('status', 'not fetched')} • {observations} observations from {matches} matches"
        else:
            status = (f"Same-match peer sample • {observations} observations from "
                      f"{peer.get('peer_accounts', 0)} players across {matches} matches")
        self.farming_compare_status.configure(text=status)
        self.farming_extra_labels["last_hits"].configure(text=fmt_number(farming.get("last_hits_avg"), 1))
        self.farming_extra_labels["denies"].configure(text=fmt_number(farming.get("denies_avg"), 1))
        high_fraction = farming.get("high_soul_fraction")
        high_text = fmt_percent(high_fraction * 100) if high_fraction is not None else "N/A"
        self.farming_extra_labels["consistency"].configure(text=high_text)
        self.farming_details_label.configure(text=(
            f"Avg souls/min: {fmt_number(farming.get('souls_per_min_avg'), 1)}  •  "
            f"Recent: {fmt_number(farming.get('recent_souls_per_min'), 1)}  •  "
            f"Avg net worth: {fmt_number(farming.get('souls_avg'), 0)}\n"
            f"Last hits/min: {fmt_number(farming.get('last_hits_per_min_avg'), 2)}  •  "
            f"Denies/min: {fmt_number(farming.get('denies_per_min_avg'), 2)}  •  "
            f"Soul data: {farming.get('souls_count', 0)}/{analysis['match_count']} matches\n"
            f"{farming.get('indicator', 'Unavailable')}  •  "
            f"High-rate threshold: {farming.get('high_soul_threshold', 1000):.0f}/min (provisional)\n"
            "Peer comparison is from shared recent matches, not a global/rank-wide benchmark. "
            "Farming contributes to the current screening score when evidence is available."
        ))

        # Top heroes.
        if self.current_top_heroes:
            hero_lines = []

            for index, hero in enumerate(
                self.current_top_heroes,
                start=1,
            ):
                matches = hero["matches"]
                wins = hero["wins"]
                losses = hero["losses"]
                win_rate = hero["win_rate"]

                if losses is None:
                    record = f"{wins} wins"
                else:
                    record = f"{wins}W / {losses}L"

                if win_rate is None:
                    rate_text = "Win rate N/A"
                else:
                    rate_text = f"{win_rate:.1f}% win rate"

                hero_lines.append(
                    f"{index}. {hero['name']}  |  "
                    f"{matches} matches  |  "
                    f"{record}  |  {rate_text}"
                )

            self.hero_text.configure(
                text="\n".join(hero_lines),
                fg=self.TEXT,
            )

        else:
            self.hero_text.configure(
                text="No hero usage data was returned.",
                fg=self.MUTED,
            )

        # Show only observations that can be substantiated from this profile.
        bullets = []
        farming = analysis.get("farming") or {}
        spm = farming.get("souls_per_min_avg")
        if spm is not None:
            bullets.append(f"• Observed farming: {fmt_number(spm, 0)} souls/min on average.")
        coverage = analysis.get("benchmark_coverage", 0)
        p90 = farming.get("benchmark_p90_matches", 0)
        if coverage:
            bullets.append(f"• Farming exceeded the general single-match P90 benchmark in {p90}/{coverage} observed matches.")
        if analysis.get("farming_component") is not None:
            bullets.append(f"• Farming anomaly component: {analysis['farming_component']:.1f}/100.")
        if analysis.get("experience_component") is not None:
            bullets.append(f"• Verified lifetime-experience mismatch component: {analysis['experience_component']:.1f}/100.")
        if analysis.get("combat_component") is not None:
            bullets.append(f"• Combat anomaly component: {analysis['combat_component']:.1f}/100.")
        if analysis.get("mechanics_component") is not None:
            bullets.append(f"• Mechanical performance component: {analysis['mechanics_component']:.1f}/100.")
        if analysis.get("consistency_component") is not None:
            bullets.append(f"• Repeated high-performance component: {analysis['consistency_component']:.1f}/100.")
        if not bullets:
            bullets.append("• Insufficient performance data to identify specific suspicion indicators.")
        self.indicators_text.configure(state="normal")
        self.indicators_text.delete("1.0", "end")
        self.indicators_text.insert("1.0", "\n".join(bullets))
        self.indicators_text.configure(state="disabled")

    def redraw_score_bar(self):
        if not self.current_analysis:
            return

        analysis = self.current_analysis

        style = self.RISK_STYLES.get(
            analysis["risk"],
            {
                "background": self.ACCENT,
            },
        )

        width = max(
            self.score_canvas.winfo_width(),
            100,
        )

        self.score_canvas.delete("all")

        self.score_canvas.create_rectangle(
            0,
            0,
            width * analysis["score"] / 100,
            16,
            fill=style["background"],
            outline="",
        )

    def draw_rate_bar(self, canvas, value, color):
        canvas.delete("all")

        width = max(
            canvas.winfo_width(),
            100,
        )

        if value is not None:
            canvas.create_rectangle(
                0,
                0,
                width * max(0, min(value, 100)) / 100,
                12,
                fill=color,
                outline="",
            )

    def set_report(self, report):
        self.report_text.configure(state="normal")
        self.report_text.delete("1.0", "end")
        self.report_text.insert("1.0", report)
        self.report_text.configure(state="disabled")

    def copy_smurf_report_description(self):
        if not self.current_analysis:
            messagebox.showinfo("No profile analyzed", "Analyze a player before copying a smurf report description.")
            return
        description = build_smurf_report_description(
            self.current_analysis, self.current_player, self.current_account_id
        )
        self.copy_text(description)
        self.status_label.configure(
            text="Smurf report description copied — ready to paste.",
            fg=self.GREEN,
        )

    def copy_report(self):
        if not self.current_report:
            return

        self.copy_text(self.current_report)

    # --------------------------------------------------------
    # Match lookup
    # --------------------------------------------------------

    def start_match_lookup(self):
        match_id = self.match_id_entry.get().strip()

        if not match_id.isdigit():
            messagebox.showwarning(
                "Invalid match ID",
                "Enter a numeric match ID.",
            )
            return

        self.set_match_status(f"Fetching match {match_id}…", self.YELLOW)

        threading.Thread(
            target=self.match_lookup_worker,
            args=(match_id,),
            daemon=True,
        ).start()

    def match_lookup_worker(self, match_id):
        try:
            # Resolve names for the first lookup, even when no profile has
            # previously been analyzed. Keep previously loaded names on failure.
            hero_names = dict(self.hero_names)
            if not hero_names:
                hero_names = get_hero_names()

            data = api_get(
                f"/v1/matches/{match_id}/metadata"
            )

            players = find_match_players(data)

            if not players:
                self.after(
                    0,
                    lambda: self.match_lookup_no_players(
                        match_id,
                        data,
                    ),
                )
                return

            enriched = []

            for original_player in players[:12]:
                player = dict(original_player)
                account_id = match_account_id(player)
                if account_id is not None:
                    player["_account_id_normalized"] = account_id
                    # The match payload can omit persona names and profile details.
                    # Fetch the public player record and merge only missing fields.
                    profile = {}
                    try:
                        profile = get_player(account_id)
                        player = merge_player_details(player, profile)
                    except Exception as exc:
                        player["_profile_error"] = str(exc)

                    # Preserve any existing match K/D/A; history is only used for
                    # screening context and recorded play time.
                    try:
                        history = get_history(account_id)
                        match_analysis = analyze_history(history)
                        try:
                            match_analysis["peer_comparison"] = compare_farming_to_peers(history, account_id)
                        except Exception as peer_exc:
                            print(f"Peer comparison unavailable for {account_id}: {peer_exc}")
                        player["_screening"] = calculate_screening_v52(
                            match_analysis, profile if isinstance(profile, dict) else {}, None
                        )
                    except Exception as exc:
                        player["_screening_error"] = str(exc)
                        print(f"Screening unavailable for {account_id}: {exc}", file=sys.stderr)

                enriched.append(player)

            self.after(
                0,
                lambda: self.show_match_players(
                    match_id,
                    data,
                    enriched,
                    hero_names,
                ),
            )

        except Exception as exc:
            error = str(exc)
            traceback.print_exc()

            self.after(
                0,
                lambda: self.match_lookup_failed(error),
            )

    def match_lookup_failed(self, error):
        self.set_match_status("Match lookup failed.", self.RED)

        messagebox.showerror(
            "Match lookup error",
            error,
        )

    def match_lookup_no_players(self, match_id, raw_json):
        self.match_raw = raw_json

        self.set_match_status(
            f"Match {match_id} metadata was returned, but no player list could be recognized. Opening diagnostic JSON.",
            self.YELLOW,
        )

        self.show_diagnostic_window(raw_json)

    def show_diagnostic_window(self, data):
        window = tk.Toplevel(self)
        window.title("Match Metadata Diagnostic")
        window.geometry("900x650")
        window.configure(bg=self.BG)

        toolbar = tk.Frame(window, bg=self.BG)
        toolbar.pack(
            fill="x",
            padx=10,
            pady=10,
        )

        ttk.Button(
            toolbar,
            text="Copy JSON",
            command=lambda: self.copy_text(
                json.dumps(data, indent=2, ensure_ascii=False)
            ),
        ).pack(side="left")

        ttk.Button(
            toolbar,
            text="Copy Diagnostic JSON",
            command=lambda: self.save_diagnostic(data),
        ).pack(side="left", padx=8)

        text_widget = tk.Text(
            window,
            wrap="none",
            bg=self.PANEL,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            font=("Consolas", 9),
        )

        text_widget.pack(
            fill="both",
            expand=True,
            padx=10,
            pady=(0, 10),
        )

        text_widget.insert(
            "1.0",
            json.dumps(data, indent=2, ensure_ascii=False),
        )

    def save_diagnostic(self, data):
        self.copy_text(
            json.dumps(data, indent=2, ensure_ascii=False)
        )

        messagebox.showinfo(
            "Diagnostic JSON copied",
            "The complete JSON has been copied to your clipboard. "
            "Paste it into Notepad and save it as a .json file.",
        )

    def show_match_players(self, match_id, raw_json, players, hero_names=None):
        if hero_names:
            self.hero_names.update(hero_names)
        self.match_raw = raw_json

        self.set_match_status(f"Match {match_id} · {len(players)} player records recognized", self.GREEN)

        for frame in self.team_frames:
            for child in frame.winfo_children()[1:]:
                child.destroy()

        teams = {}

        for player in players:
            team = first_value(
                player,
                (
                    "team",
                    "team_number",
                    "team_id",
                    "team_num",
                ),
            )

            teams.setdefault(str(team), []).append(player)

        if len(teams) == 2:
            groups = list(teams.values())

        elif len(teams) > 2:
            groups = list(teams.values())[:2]

        else:
            split = (len(players) + 1) // 2
            groups = [
                players[:split],
                players[split:],
            ]

        for index in range(2):
            group = groups[index] if index < len(groups) else []

            for player in group:
                self.create_match_card(
                    self.team_frames[index],
                    player,
                )

            if not group:
                tk.Label(
                    self.team_frames[index],
                    text="No player records found.",
                    bg=self.PANEL,
                    fg=self.MUTED,
                ).pack(
                    anchor="w",
                    pady=6,
                )

    def set_match_status(self, message, color=None):
        """A read-only Entry allows copying the match ID and record count."""
        self.match_status.configure(state="normal")
        self.match_status.delete(0, tk.END)
        self.match_status.insert(0, str(message))
        self.match_status.configure(state="readonly", fg=color or self.MUTED)

    def create_match_card(self, parent, player):
        """One selectable multiline Text per card; drag across lines or Ctrl+A/Ctrl+C."""
        card = tk.Frame(parent, bg=self.PANEL_ALT, padx=9, pady=8)
        card.pack(fill="x", pady=3)
        name = first_value(player, (
            "personaname", "player_name", "name", "persona_name", "steam_name"
        ))
        account_id = player.get("_account_id_normalized") or match_account_id(player)
        hero_id = first_value(player, ("hero_id", "heroId"))
        hero_name = (self.hero_names.get(str(hero_id), f"Hero ID {hero_id}")
                     if hero_id is not None else "Unknown hero")
        if not name:
            name = f"Account {account_id}" if account_id is not None else "Unknown player"
        screening = player.get("_screening") or {}
        observed_hours = screening.get("hours")
        hours_line = (f"Deadlock observed match time: {observed_hours:.1f} hours (not lifetime)"
                      if observed_hours is not None else "Deadlock observed match time: unavailable")
        kills = get_player_stat(player, KILL_FIELDS)
        deaths = get_player_stat(player, DEATH_FIELDS)
        assists = get_player_stat(player, ASSIST_FIELDS)
        kda_text = (f"{int(kills)}/{int(deaths)}/{int(assists)}"
                    if None not in (kills, deaths, assists) else "N/A")
        lines = [(str(name), "name"), (hours_line, "hours"),
                 (f"{hero_name}  ·  K/D/A: {kda_text}", "muted")]
        if screening:
            farming = screening.get("farming") or {}
            spm = farming.get("souls_per_min_avg")
            benchmark = load_general_benchmark()
            metrics = (benchmark or {}).get("metrics") or {}
            node = metrics.get("net_worth_per_min") or {}
            # Use the global single-match mean as a reference only; this is not
            # a rank-adjusted player-average comparison.
            global_mean = node.get("avg")
            try:
                spm_num = float(spm)
                global_mean = float(global_mean)
                farm_tag = "farm_high" if spm_num > global_mean else "muted"
            except (TypeError, ValueError):
                farm_tag = "muted"
            lines.append((f"Souls/min: {fmt_number(spm, 0)}", farm_tag))
            lines.append((f"Last hits: {fmt_number(farming.get('last_hits_avg'), 1)}  ·  "
                          f"Denies: {fmt_number(farming.get('denies_avg'), 1)}", "muted"))
            lines.append((f"Screening: {screening['score']}/100 ({screening['risk']})  ·  "
                          f"Confidence: {screening.get('confidence', 'Unknown')}",
                          "warning" if screening['risk'] in ('Elevated', 'High') else "muted"))
        elif player.get("_screening_error"):
            lines.append(("Screening unavailable: profile history request failed", "warning"))
        if account_id is None:
            lines.append(("Profile unavailable (no account ID)", "warning"))
        text = tk.Text(card, wrap="word", height=len(lines), bg=self.PANEL_ALT,
                       fg=self.TEXT, insertbackground=self.TEXT, relief="flat", bd=0,
                       highlightthickness=0, font=("Segoe UI", 9),
                       padx=0, pady=0, spacing1=2, spacing3=2,
                       exportselection=False, cursor="xterm")
        text.pack(fill="x")
        text.tag_configure("name", foreground=self.TEXT, font=("Segoe UI", 10, "bold"))
        text.tag_configure("hours", foreground=self.ACCENT, font=("Segoe UI", 9, "bold"))
        text.tag_configure("muted", foreground=self.MUTED)
        text.tag_configure("farm_high", foreground=self.GREEN, font=("Segoe UI", 9, "bold"))
        text.tag_configure("warning", foreground=self.YELLOW)
        for index, (line, tag) in enumerate(lines):
            if index:
                text.insert(tk.END, "\n")
            text.insert(tk.END, line, tag)
        text.configure(state="disabled")
        text.bind("<Control-a>", lambda e, t=text: self.select_match_card_text(e, t))
        text.bind("<Control-A>", lambda e, t=text: self.select_match_card_text(e, t))
        self.add_help(text, content=(
            "MATCH DETAILS\nDrag to select across multiple lines, or use Ctrl+A then Ctrl+C.\n"
            "Deadlock observed time counts only returned matches, not lifetime playtime.\n"
            "Green souls/min indicates above the available general benchmark's single-match mean. "
            "The comparison is not rank-adjusted and does not establish a player percentile.\n"
            "Screening measures suspicion; confidence measures available supporting evidence."))
        if account_id is None:
            return
        url = steam_profile_url(as_int(account_id))
        ttk.Button(card, text="Copy + Analyze Profile",
                   command=lambda target=url: self.analyze_match_profile(target)).pack(anchor="w", pady=(6, 2))
        ttk.Button(card, text="Copy Steam profile URL",
                   command=lambda target=url: self.copy_text(target)).pack(anchor="w", pady=(2, 0))

    @staticmethod
    def select_match_card_text(event, widget):
        widget.tag_add("sel", "1.0", "end-1c")
        widget.mark_set("insert", "1.0")
        return "break"

    def analyze_match_profile(self, steam_url):
        """Copy a match participant's Steam URL and analyze that account."""
        self.copy_text(steam_url)
        self.profile_entry.delete(0, tk.END)
        self.profile_entry.insert(0, steam_url)
        if str(self.lookup_button.cget("state")) == "disabled":
            self.status_label.configure(
                text="Steam profile copied. Wait for the current analysis to finish, then select Analyze Profile.",
                fg=self.YELLOW,
            )
            return
        self.start_lookup()

    def copy_match_json(self):
        if self.match_raw is None:
            messagebox.showinfo(
                "No match data",
                "Look up a match first.",
            )
            return

        self.copy_text(
            json.dumps(
                self.match_raw,
                indent=2,
                ensure_ascii=False,
            )
        )

    # --------------------------------------------------------
    # Integrated diagnostic console
    # --------------------------------------------------------

    def build_console_tab(self):
        bar = tk.Frame(self.console_tab, bg=self.BG)
        bar.pack(fill="x", padx=10, pady=8)
        ttk.Button(bar, text="Copy Console", command=self.copy_console).pack(side="left")
        ttk.Button(bar, text="Clear Console", command=self.clear_console).pack(side="left", padx=8)
        area = tk.Frame(self.console_tab, bg=self.BG)
        area.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        scroll = ttk.Scrollbar(area)
        scroll.pack(side="right", fill="y")
        self.console_text = tk.Text(area, wrap="word", bg=self.PANEL, fg=self.TEXT,
                                    insertbackground=self.TEXT, relief="flat",
                                    padx=12, pady=12, font=("Consolas", 10),
                                    yscrollcommand=scroll.set)
        self.console_text.pack(side="left", fill="both", expand=True)
        scroll.configure(command=self.console_text.yview)
        self.console_text.tag_configure("error", foreground=self.RED)
        self.console_text.configure(state="disabled")

    def install_console_capture(self):
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        self._stdout_redirect = GuiStream(self.console_queue, "info", self._original_stdout)
        self._stderr_redirect = GuiStream(self.console_queue, "error", self._original_stderr)
        sys.stdout = self._stdout_redirect
        sys.stderr = self._stderr_redirect
        print("Deadlock Profile Analyzer v0.5.8 — diagnostics console ready.")

    def drain_console_queue(self):
        chunks = []
        try:
            while len(chunks) < 250:
                chunks.append(self.console_queue.get_nowait())
        except queue.Empty:
            pass
        if chunks:
            self.console_text.configure(state="normal")
            for level, chunk in chunks:
                self.console_text.insert(tk.END, chunk, "error" if level == "error" else ())
            self.console_text.configure(state="disabled")
            self.console_text.see(tk.END)
        self.after(80, self.drain_console_queue)

    def copy_console(self):
        self.copy_text(self.console_text.get("1.0", "end-1c"))

    def clear_console(self):
        self.console_text.configure(state="normal")
        self.console_text.delete("1.0", tk.END)
        self.console_text.configure(state="disabled")

    def _scroll_report(self, event):
        if getattr(event, "num", None) in (4, 5):
            direction = -1 if event.num == 4 else 1
        else:
            delta = getattr(event, "delta", 0)
            if not delta:
                return
            direction = -max(1, abs(delta) // 120) if delta > 0 else max(1, abs(delta) // 120)
        self.report_text.yview_scroll(direction * 3, "units")
        return "break"

    # --------------------------------------------------------
    # Clipboard helper
    # --------------------------------------------------------

    def copy_text(self, value):
        self.clipboard_clear()
        self.clipboard_append(value)
        self.update_idletasks()

        self.status_label.configure(
            text="Copied to clipboard.",
            fg=self.GREEN,
        )


# ============================================================
# APPLICATION ENTRY POINT
# ============================================================

if __name__ == "__main__":
    try:
        configure_file_logging()
        install_error_handlers()
        APP_LOGGER.info("Starting Deadlock Profile Analyzer %s; frozen=%s", APP_VERSION, getattr(sys, "frozen", False))
        app = DeadlockAnalyzer()
        app.mainloop()
    except Exception:
        report_uncaught_error(*sys.exc_info())
        sys.exit(1)
