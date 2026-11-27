"""Flashscore live tennis feed.

The public scoreboard feed is ``f_2_0_3_en_1`` (sport id 2 is tennis) on
``local-global.flashscore.ninja``. Records are separated by ``~``. Fields
inside a record are separated by ``¬`` and keyed with ``÷``.

``~ZA`` is a tournament. ``~AA`` is one match. Live matches use ``AB=2``.
``AC`` 17–21 are the set currently being played (1st through 5th). Set games
are ``BA/BB``, ``BC/BD``, ``BE/BF``, ``BG/BH``, ``BI/BJ`` (home/away). ``AG``
and ``AH`` are sets won. ``WC`` is who is serving (1 home, 2 away).
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from src.clients.sofascore_client import LiveTennisMatch, _sets_won

FIELD = "\u00ac"  # ¬
KV = "\u00f7"  # ÷

# AC while the match is in progress. 17 is the 1st set, documented by
# Flashscore's own stage list (FIRST_SET, SECOND_SET, ...).
_LIVE_SET_STAGE = {17: 1, 18: 2, 19: 3, 20: 4, 21: 5}
_HOME_GAMES = ("BA", "BC", "BE", "BG", "BI")
_AWAY_GAMES = ("BB", "BD", "BF", "BH", "BJ")

FLASHSCORE_URLS = (
    "https://local-global.flashscore.ninja/2/x/feed/f_2_0_3_en_1",
    "https://d.flashscore.com/x/feed/f_2_0_3_en_1",
)
DEFAULT_FSIGN = "SW9D1eZo"


def flashscore_headers() -> Dict[str, str]:
    fsign = os.environ.get("FLASHSCORE_FSIGN", "").strip() or DEFAULT_FSIGN
    return {
        "x-fsign": fsign,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.flashscore.com/",
        "Accept": "*/*",
    }


def _fields(section: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in section.split(FIELD):
        if KV not in part:
            continue
        key, value = part.split(KV, 1)
        if key:
            out[key] = value
    return out


def _games(fields: Dict[str, str], key: str) -> Optional[int]:
    raw = fields.get(key)
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
        return value if value >= 0 else None
    except ValueError:
        return None


def _set_scores(fields: Dict[str, str]) -> tuple:
    home: Dict[str, int] = {}
    away: Dict[str, int] = {}
    for index, (home_key, away_key) in enumerate(zip(_HOME_GAMES, _AWAY_GAMES), start=1):
        home_games = _games(fields, home_key)
        away_games = _games(fields, away_key)
        if home_games is None or away_games is None:
            continue
        home[f"period{index}"] = home_games
        away[f"period{index}"] = away_games
    return home, away


def _sets_won_value(fields: Dict[str, str], key: str, home: Dict[str, int], away: Dict[str, int], side: str) -> int:
    raw = fields.get(key)
    if raw is not None and raw != "":
        try:
            return int(raw)
        except ValueError:
            pass
    shaped = {"homeScore": home, "awayScore": away}
    if side == "home":
        return _sets_won(shaped, "homeScore", "awayScore")
    return _sets_won(shaped, "awayScore", "homeScore")


def _is_doubles(tournament: str, home: str, away: str) -> bool:
    label = tournament.lower()
    if "double" in label:
        return True
    return "/" in home or "/" in away


def parse_flashscore_board(raw: str) -> Optional[List[LiveTennisMatch]]:
    """Return None for an unrecognizable board, or the list of live singles."""
    if not raw or raw.lstrip().startswith("<"):
        return None
    tournament = ""
    seen_match = False
    incomplete_live = False
    tennis_header = False
    matches: List[LiveTennisMatch] = []
    for section in raw.strip().split("~"):
        fields = _fields(section)
        if "SA" in fields:
            if fields["SA"] != "2":
                return None
            tennis_header = True
        if "ZA" in fields:
            tournament = fields["ZA"]
        if not fields.get("AA") or not fields.get("AB") or not (tournament or tennis_header):
            continue
        if not fields.get("AE") or not fields.get("AF"):
            continue
        seen_match = True
        match = _one_match(fields, tournament)
        if match is not None:
            matches.append(match)
        elif fields["AB"] == "2" and not _is_doubles(tournament, fields["AE"], fields["AF"]):
            incomplete_live = True
    if incomplete_live and not matches:
        return None
    if seen_match or raw.strip().strip("~¬") == "SA÷2":
        return matches
    return None


def matches_from_flashscore_feed(raw: str) -> List[LiveTennisMatch]:
    """In-progress singles matches from one Flashscore feed body."""
    return parse_flashscore_board(raw) or []


def _one_match(fields: Dict[str, str], tournament: str) -> Optional[LiveTennisMatch]:
    current = _LIVE_SET_STAGE.get(_games(fields, "AC"))
    if fields.get("AB") != "2" or current is None:
        return None
    home_name = (fields.get("AE") or "").strip()
    away_name = (fields.get("AF") or "").strip()
    if not home_name or not away_name:
        return None
    if _is_doubles(tournament, home_name, away_name):
        return None
    home_score, away_score = _set_scores(fields)
    if f"period{current}" not in home_score:
        return None
    description = f"set {current}"
    return LiveTennisMatch(
        home_name=home_name,
        away_name=away_name,
        current_set=current,
        status_type="inprogress",
        status_description=description,
        round_name=tournament,
        home_sets_won=_sets_won_value(fields, "AG", home_score, away_score, "home"),
        away_sets_won=_sets_won_value(fields, "AH", home_score, away_score, "away"),
        home_score=home_score,
        away_score=away_score,
        raw=fields,
    )
