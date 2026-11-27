"""Map ESPN's public tennis scoreboard into LiveTennisMatch rows.

No API key. Each scoreboard event is a tournament; matches sit under
``groupings[].competitions[]``. Only ``status.type.state == "in"`` is live.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from src.clients.sofascore_client import LiveTennisMatch, _sets_won

logger = logging.getLogger(__name__)


def _games(linescores) -> Dict[str, int]:
    scores: Dict[str, int] = {}
    for index, line in enumerate(linescores or [], start=1):
        if not isinstance(line, dict):
            continue
        try:
            scores[f"period{index}"] = int(float(line.get("value")))
        except (TypeError, ValueError):
            continue
    return scores


def _player_name(competitor: dict) -> str:
    athlete = competitor.get("athlete") or {}
    return str(athlete.get("displayName") or athlete.get("fullName") or "").strip()


def _round_name(competition: dict) -> str:
    raw = competition.get("round")
    if isinstance(raw, dict):
        return str(raw.get("displayName") or raw.get("name") or raw.get("description") or "")
    if raw:
        return str(raw)
    notes = competition.get("notes") or []
    if notes and isinstance(notes[0], dict):
        return str(notes[0].get("headline") or notes[0].get("text") or "")
    return ""


def _one_match(competition: dict) -> Optional[LiveTennisMatch]:
    status = competition.get("status") or {}
    kind = status.get("type") or {}
    if kind.get("state") != "in":
        return None
    sides = {}
    for competitor in competition.get("competitors") or []:
        if not isinstance(competitor, dict):
            continue
        side = competitor.get("homeAway")
        name = _player_name(competitor)
        if side in {"home", "away"} and name:
            sides[side] = (name, competitor)
    if "home" not in sides or "away" not in sides:
        return None
    home_name, home = sides["home"]
    away_name, away = sides["away"]
    if "/" in home_name or "/" in away_name:
        return None
    home_score = _games(home.get("linescores"))
    away_score = _games(away.get("linescores"))
    if not home_score and not away_score:
        # In progress with no games recorded. The strategy must not invent a lead.
        return None
    try:
        current = int(status.get("period") or 0)
    except (TypeError, ValueError):
        current = 0
    if current <= 0:
        current = max(len(home_score), len(away_score))
    shaped = {"homeScore": home_score, "awayScore": away_score}
    description = str(kind.get("shortDetail") or kind.get("detail") or kind.get("description") or "")
    return LiveTennisMatch(
        home_name=home_name,
        away_name=away_name,
        current_set=current,
        status_type="inprogress",
        status_description=description,
        round_name=_round_name(competition),
        home_sets_won=_sets_won(shaped, "homeScore", "awayScore"),
        away_sets_won=_sets_won(shaped, "awayScore", "homeScore"),
        home_score=home_score,
        away_score=away_score,
        raw=competition,
    )


def matches_from_espn_scoreboard(payload: dict) -> List[LiveTennisMatch]:
    """In-progress singles matches from one ATP or WTA scoreboard document."""
    if not isinstance(payload, dict):
        return []
    matches: List[LiveTennisMatch] = []
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        for grouping in event.get("groupings") or []:
            if not isinstance(grouping, dict):
                continue
            for competition in grouping.get("competitions") or []:
                if not isinstance(competition, dict):
                    continue
                try:
                    match = _one_match(competition)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Skipping malformed ESPN match: %s", exc)
                    continue
                if match is not None:
                    matches.append(match)
    return matches
