"""
SofaScore live-tennis client.

Kalshi's API exposes prices and volume but NOT live match state (which set a
match is in). SofaScore's public feed does. This lightweight client fetches
in-progress tennis matches and exposes the current set number plus both
players' names so a strategy can answer "is <player> currently in set 3?".

Endpoint (public, unofficial):
    GET https://api.sofascore.com/api/v1/sport/tennis/events/live

Response shape (trimmed):
    {
      "events": [
        {
          "homeTeam": {"name": "Alcaraz C.", "shortName": "Alcaraz C.", "slug": "alcaraz-carlos"},
          "awayTeam": {"name": "Sinner J.", ...},
          "homeScore": {"current": 1, "period1": 6, "period2": 4, "period3": 2},
          "awayScore": {"current": 1, "period1": 4, "period2": 6, "period3": 1},
          "status":    {"code": 7, "description": "2nd set", "type": "inprogress"},
          ...
        },
        ...
      ]
    }

Be polite with request frequency — SofaScore will IP-block aggressive polling.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# SofaScore sits behind Cloudflare, which blocks plain HTTP clients (httpx,
# requests, even curl) by TLS/HTTP2 fingerprint — they get 403. curl_cffi
# impersonates a real Chrome fingerprint and gets through.
from curl_cffi.requests import AsyncSession

logger = logging.getLogger(__name__)

SOFASCORE_LIVE_TENNIS_URL = "https://api.sofascore.com/api/v1/sport/tennis/events/live"

# Status descriptions that mean a match is NOT actively being played right now
# even though SofaScore may still flag it "inprogress" (rain/medical stoppage,
# retirement, walkover, etc.). We refuse to trade these.
_NOT_LIVE_HINTS = (
    "suspend", "interrupt", "cancel", "postpon", "retir",
    "walkover", "delay", "abandon", "await", "pause",
)

# Browser profile curl_cffi impersonates (TLS + HTTP2 + header order).
_IMPERSONATE = "chrome"

# Extra headers to look like a fetch from the SofaScore web app.
_DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
}

_TOKEN_RE = re.compile(r"[^a-z]+")


def _name_tokens(name: str) -> List[str]:
    """Break a player name into comparable lowercase, accent-free tokens.

    Drops single-letter initials (e.g. the "C" in "Alcaraz C.") so that a
    Kalshi name like "Carlos Alcaraz" and a SofaScore name like "Alcaraz C."
    both reduce to the shared surname token {"alcaraz"}. Accents are stripped
    so "Nedić" and "Nedic" compare equal.
    """
    if not name:
        return []
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = _TOKEN_RE.sub(" ", ascii_name.lower())
    return [t for t in cleaned.split() if len(t) >= 2]


def names_match(name_a: str, name_b: str) -> bool:
    """True if two player names plausibly refer to the same person.

    SURNAME-based (the last significant token), NOT any-token overlap — so it
    no longer matches on shared FIRST names ("Elena" Rybakina vs "Elena"
    Milovanovic) or on a surname colliding with another's first name. This is
    deliberately permissive about same-surname players (e.g. two "Wang"s); the
    caller disambiguates by requiring BOTH players of a match to line up
    (see TennisSet3Strategy._live_match_for_market).
    """
    a = _name_tokens(name_a)
    b = _name_tokens(name_b)
    if not a or not b:
        return False
    surname_a, surname_b = a[-1], b[-1]
    if surname_a == surname_b:
        return True
    # Handle multi-part surnames represented differently across sources
    # (e.g. "Van Assche" vs "Assche"): a surname appearing among the other's
    # tokens still counts, but a bare first-name token never will.
    return surname_a in b or surname_b in a


@dataclass
class LiveTennisMatch:
    """A single in-progress tennis match from SofaScore."""

    home_name: str
    away_name: str
    current_set: int
    status_type: str
    status_description: str
    home_sets_won: int = 0
    away_sets_won: int = 0
    home_ranking: Optional[int] = None
    away_ranking: Optional[int] = None
    home_score: Dict = field(default_factory=dict, repr=False)
    away_score: Dict = field(default_factory=dict, repr=False)
    raw: Dict = field(default_factory=dict, repr=False)

    @property
    def in_progress(self) -> bool:
        return self.status_type == "inprogress"

    @property
    def is_live(self) -> bool:
        """True only if the match is actively being played right now.

        Stricter than ``in_progress``: SofaScore keeps a match flagged
        "inprogress" while it is suspended/interrupted (rain, medical timeout)
        or after a retirement/walkover. Those are excluded so we never trade a
        game that isn't actually live.
        """
        if self.status_type != "inprogress":
            return False
        desc = (self.status_description or "").lower()
        return not any(hint in desc for hint in _NOT_LIVE_HINTS)

    def score_label(self) -> str:
        """Compact live score for logs, e.g. '6-4 4-3'."""
        parts = []
        for n in range(1, max(self.current_set, 1) + 1):
            h = self._games("home", n)
            a = self._games("away", n)
            if h is None or a is None:
                continue
            parts.append(f"{h}-{a}")
        return " ".join(parts) if parts else "?"

    def player_names(self) -> List[str]:
        return [self.home_name, self.away_name]

    def matches_player(self, name: str) -> bool:
        return names_match(name, self.home_name) or names_match(name, self.away_name)

    # -- per-set helpers (for the "2-0 sweep in progress" rule) --------------

    def _side_of(self, name: str) -> Optional[str]:
        if names_match(name, self.home_name):
            return "home"
        if names_match(name, self.away_name):
            return "away"
        return None

    def _games(self, side: str, set_no: int) -> Optional[int]:
        score = self.home_score if side == "home" else self.away_score
        try:
            return int(score.get(f"period{set_no}"))
        except (TypeError, ValueError):
            return None

    def _completed_set_winner(self, set_no: int) -> Optional[str]:
        """Return 'home'/'away' if that set looks finished and won, else None.

        A tennis set is complete when the leader has >= 6 games with a 2-game
        margin, or won a tiebreak 7-6.
        """
        h = self._games("home", set_no)
        a = self._games("away", set_no)
        if h is None or a is None:
            return None
        hi, lo = max(h, a), min(h, a)
        if hi >= 6 and (hi - lo >= 2 or hi == 7):
            return "home" if h > a else "away"
        return None

    def won_set(self, name: str, set_no: int) -> bool:
        side = self._side_of(name)
        return side is not None and self._completed_set_winner(set_no) == side

    def games_ahead_in_set(self, name: str, set_no: int) -> Optional[int]:
        """Games ``name`` is ahead by in a set (negative if behind). None if unknown."""
        side = self._side_of(name)
        if side is None:
            return None
        h = self._games("home", set_no)
        a = self._games("away", set_no)
        if h is None or a is None:
            return None
        mine, theirs = (h, a) if side == "home" else (a, h)
        return mine - theirs

    def leading_set(self, name: str, set_no: int) -> bool:
        """True if ``name`` is currently ahead on games in the given set."""
        ahead = self.games_ahead_in_set(name, set_no)
        return ahead is not None and ahead > 0

    def leading_set_by(self, name: str, set_no: int, margin: int = 2) -> bool:
        """True if ``name`` leads the given set by at least ``margin`` games."""
        ahead = self.games_ahead_in_set(name, set_no)
        return ahead is not None and ahead >= margin

    def two_set_sweep_in_progress(self, name: str, min_lead: int = 2) -> bool:
        """True if ``name`` won set 1 and leads set 2 by at least ``min_lead`` games.

        Captures the "on track for a straight-sets win" case that never
        reaches set 3. A 1-game lead is not enough — we require being up by 2.
        """
        return (
            self.current_set == 2
            and self.won_set(name, 1)
            and self.leading_set_by(name, 2, min_lead)
        )

    # -- ranking / live-margin helpers (for the set-3 "upset" overlay) -------

    def ranking_of(self, name: str) -> Optional[int]:
        """ATP/WTA singles ranking for ``name`` (None if unranked/unknown)."""
        side = self._side_of(name)
        if side == "home":
            return self.home_ranking
        if side == "away":
            return self.away_ranking
        return None

    def sets_won_by(self, name: str) -> Optional[int]:
        side = self._side_of(name)
        if side == "home":
            return self.home_sets_won
        if side == "away":
            return self.away_sets_won
        return None

    def current_set_games(self, name: str) -> Optional[int]:
        """Games ``name`` has in the CURRENT set (None if unknown)."""
        side = self._side_of(name)
        if side is None or self.current_set < 1:
            return None
        return self._games(side, self.current_set)

    def games_deficit_current_set(self, name: str) -> Optional[int]:
        """How many games ``name`` is BEHIND by in the current set.

        Positive => behind, negative => ahead, 0 => level. None if unknown.
        """
        side = self._side_of(name)
        if side is None or self.current_set < 1:
            return None
        mine = self._games(side, self.current_set)
        theirs = self._games("away" if side == "home" else "home", self.current_set)
        if mine is None or theirs is None:
            return None
        return theirs - mine


def _current_set_number(event: Dict) -> int:
    """Return the highest set index that has a score recorded (1-based).

    In tennis, the current set is the highest ``periodN`` present in either
    player's score object. If none are present the match hasn't started a set.
    """
    max_set = 0
    for score_key in ("homeScore", "awayScore"):
        score = event.get(score_key) or {}
        for key in score:
            if key.startswith("period") and key[6:].isdigit():
                max_set = max(max_set, int(key[6:]))
    return max_set


def _parse_ranking(raw) -> Optional[int]:
    """Normalize a SofaScore ATP/WTA ranking to an int, or None if unranked.

    SofaScore reports singles ranking as ``homeTeam.ranking`` / ``awayTeam.ranking``.
    Missing, null, or non-positive values (0) mean the player is effectively
    unranked, which we represent as None.
    """
    try:
        rank = int(raw)
    except (TypeError, ValueError):
        return None
    return rank if rank > 0 else None


def _sets_won(event: Dict, score_key: str, opp_key: str) -> int:
    """Count sets a player has won (their period score strictly higher)."""
    score = event.get(score_key) or {}
    opp = event.get(opp_key) or {}
    won = 0
    for key in score:
        if key.startswith("period") and key[6:].isdigit():
            try:
                mine = int(score.get(key))
                theirs = int(opp.get(key, 0))
            except (TypeError, ValueError):
                continue
            # A completed set generally ends at >= 6 games with a 2-game margin
            # (or a tiebreak at 7). Use a simple "higher and >= 6" heuristic.
            if mine > theirs and mine >= 6:
                won += 1
    return won


class SofaScoreClient:
    """Minimal async client for SofaScore live tennis data (via curl_cffi)."""

    def __init__(self, timeout: float = 15.0, session: Optional[AsyncSession] = None):
        self.timeout = timeout
        self._owns_session = session is None
        self.session = session or AsyncSession()

    async def get_live_tennis_matches(self) -> List[LiveTennisMatch]:
        """Fetch all in-progress live tennis matches.

        Returns an empty list on any network/parse error so a caller can
        degrade gracefully rather than crash the trading loop.
        """
        try:
            resp = await self.session.get(
                SOFASCORE_LIVE_TENNIS_URL,
                headers=_DEFAULT_HEADERS,
                impersonate=_IMPERSONATE,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - never let SofaScore kill the loop
            logger.warning("SofaScore live fetch failed: %s", exc)
            return []

        events = data.get("events", []) if isinstance(data, dict) else []
        matches: List[LiveTennisMatch] = []
        for ev in events:
            try:
                home_team = ev.get("homeTeam") or {}
                away_team = ev.get("awayTeam") or {}
                home = home_team.get("name", "") or ""
                away = away_team.get("name", "") or ""
                status = ev.get("status") or {}
                match = LiveTennisMatch(
                    home_name=home,
                    away_name=away,
                    current_set=_current_set_number(ev),
                    status_type=status.get("type", ""),
                    status_description=status.get("description", ""),
                    home_sets_won=_sets_won(ev, "homeScore", "awayScore"),
                    away_sets_won=_sets_won(ev, "awayScore", "homeScore"),
                    home_ranking=_parse_ranking(home_team.get("ranking")),
                    away_ranking=_parse_ranking(away_team.get("ranking")),
                    home_score=ev.get("homeScore") or {},
                    away_score=ev.get("awayScore") or {},
                    raw=ev,
                )
                if home and away:
                    matches.append(match)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed SofaScore event: %s", exc)
                continue

        logger.info("SofaScore: %d live tennis matches fetched", len(matches))
        return matches

    async def find_match_for_player(self, player_name: str) -> Optional[LiveTennisMatch]:
        """Return the live match involving ``player_name``, if any."""
        matches = await self.get_live_tennis_matches()
        for m in matches:
            if m.matches_player(player_name):
                return m
        return None

    async def close(self) -> None:
        if self._owns_session:
            try:
                await self.session.close()
            except Exception:  # noqa: BLE001
                pass

    async def __aenter__(self) -> "SofaScoreClient":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
