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
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# SofaScore sits behind Cloudflare, which blocks plain HTTP clients (httpx,
# requests, even curl) by TLS/HTTP2 fingerprint — they get 403. curl_cffi
# impersonates a real Chrome fingerprint and gets through.
from curl_cffi.requests import AsyncSession

logger = logging.getLogger(__name__)

# Tried in order. api.sofascore.com is the documented host. www.sofascore.com/api
# is what the site itself calls. api.sofascore.app is a sibling edge. A
# datacenter IP is often Turnstile-challenged on all of them; the proxy below
# is the way through when every host returns 403.
SOFASCORE_LIVE_URLS = (
    "https://api.sofascore.com/api/v1/sport/tennis/events/live",
    "https://www.sofascore.com/api/v1/sport/tennis/events/live",
    "https://api.sofascore.app/api/v1/sport/tennis/events/live",
)
SOFASCORE_LIVE_TENNIS_URL = SOFASCORE_LIVE_URLS[0]

# Status descriptions that mean a match is NOT actively being played right now
# even though SofaScore may still flag it "inprogress" (rain/medical stoppage,
# retirement, walkover, etc.). We refuse to trade these.
_NOT_LIVE_HINTS = (
    "suspend", "interrupt", "cancel", "postpon", "retir",
    "walkover", "delay", "abandon", "await", "pause",
)

# Let curl_cffi set the User-Agent that belongs to this TLS fingerprint.
# A hardcoded older UA on top of a newer impersonation is itself a 403.
_IMPERSONATE_TARGETS = ("chrome152", "chrome131", "chrome")

# A browser fetch from the SofaScore site.
_DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.sofascore.com",
    "Referer": "https://www.sofascore.com/",
}

# After a 403, skip SofaScore for a growing pause. ESPN still runs every cycle.
_SOFASCORE_BACKOFF_START = 30.0
_SOFASCORE_BACKOFF_MAX = 300.0
_sofascore_retry_at = 0.0
_sofascore_backoff = _SOFASCORE_BACKOFF_START

ESPN_SCOREBOARDS = (
    "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard",
    "https://site.api.espn.com/apis/site/v2/sports/tennis/wta/scoreboard",
)

_TOKEN_RE = re.compile(r"[^a-z]+")


def _split_name(name: str) -> tuple:
    """Return ``(tokens of length >= 2, single-letter initials)``.

    Accents are stripped so "Nedić" and "Nedic" compare equal. Initials are
    kept separately: "Alcaraz C." must still be able to agree with "Carlos
    Alcaraz", and two "Wang"s must be told apart by "Y." vs "X.".
    """
    if not name:
        return [], []
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = _TOKEN_RE.sub(" ", ascii_name.lower())
    tokens: List[str] = []
    initials: List[str] = []
    for piece in cleaned.split():
        if len(piece) >= 2:
            tokens.append(piece)
        elif len(piece) == 1:
            initials.append(piece)
    return tokens, initials


def _name_tokens(name: str) -> List[str]:
    """Significant (length >= 2) tokens of a player name. Initials are dropped."""
    tokens, _initials = _split_name(name)
    return tokens


def _given_names_agree(
    given_a: List[str],
    given_b: List[str],
    initials_a: List[str],
    initials_b: List[str],
) -> bool:
    """True when both sides have a given-name signal and those signals agree."""
    if not (given_a or initials_a) or not (given_b or initials_b):
        return False
    if set(given_a) & set(given_b):
        return True
    for token in given_a:
        if token[0] in initials_b:
            return True
    for token in given_b:
        if token[0] in initials_a:
            return True
    return bool(set(initials_a) & set(initials_b))


def names_match(name_a: str, name_b: str, *, allow_surname_only: bool = True) -> bool:
    """True if two player names plausibly refer to the same person.

    Matching is surname-based (the last significant token), not any-token
    overlap, so "Elena Rybakina" does not match "Elena Milovanovic".

    When the surnames are equal and both names carry a given name or initial,
    those must agree ("Yafan Wang" matches "Wang Y." and not "Wang X.").
    ``allow_surname_only`` controls a bare surname ("Wang", "Assche") with no
    given-name signal. Callers pass False when the two players in a match
    share a surname, because a surname-only hit cannot tell them apart.
    """
    tokens_a, initials_a = _split_name(name_a)
    tokens_b, initials_b = _split_name(name_b)
    if not tokens_a or not tokens_b:
        return False
    surname_a, surname_b = tokens_a[-1], tokens_b[-1]
    given_a, given_b = tokens_a[:-1], tokens_b[:-1]
    if surname_a == surname_b:
        if _given_names_agree(given_a, given_b, initials_a, initials_b):
            return True
        # Both sides named a given name and they disagree.
        if (given_a or initials_a) and (given_b or initials_b):
            return False
        return allow_surname_only
    # Multi-part surnames written differently ("Van Assche" vs "Assche"):
    # the surname token appears among the other name's tokens. A shared
    # first name never reaches here, because the surnames differ.
    return surname_a in tokens_b or surname_b in tokens_a


def players_share_surname(name_a: str, name_b: str) -> bool:
    """True when both names reduce to the same surname token."""
    tokens_a, _ = _split_name(name_a)
    tokens_b, _ = _split_name(name_b)
    return bool(tokens_a and tokens_b and tokens_a[-1] == tokens_b[-1])


# SofaScore cupRoundType is the number of ties in a full knockout round.
_CUP_ROUND_KIND = {
    1: "final",
    2: "semifinal",
    4: "quarterfinal",
    8: "earlier",
    16: "earlier",
    32: "earlier",
    64: "earlier",
    230: "final",  # qualifying final
}
_ROUND_ALIASES = {
    "sf": "semifinal",
    "semifinal": "semifinal",
    "semifinals": "semifinal",
    "semi final": "semifinal",
    "semi finals": "semifinal",
    "final": "final",
    "finals": "final",
    "the final": "final",
    "grand final": "final",
    "championship": "final",
    "championships": "final",
    "championship match": "final",
    "title match": "final",
    "qf": "quarterfinal",
    "quarterfinal": "quarterfinal",
    "quarterfinals": "quarterfinal",
    "quarter final": "quarterfinal",
    "quarter finals": "quarterfinal",
}


def _normalize_round_text(label: str) -> str:
    text = re.sub(r"[_\-]+", " ", str(label or ""))
    return re.sub(r"\s+", " ", text).strip().lower()


def classify_round_label(label: str) -> Optional[str]:
    """Map a round name or slug to semifinal, final, quarterfinal, or earlier.

    Returns None when the text is not a round we recognize. "ATP Finals" is a
    tournament name, not the final round. "SF" and "semifinals" are semifinals.
    """
    text = _normalize_round_text(label)
    if not text:
        return None
    if text in _ROUND_ALIASES:
        return _ROUND_ALIASES[text]
    # Quarterfinal contains the letters "final". Match it before any final rule
    # so "Quarterfinal" and "Quarter-final" are not treated as the final.
    if re.search(r"\bquarter\s*finals?\b", text):
        return "quarterfinal"
    if re.search(r"\bsemi\s*finals?\b", text) or re.search(r"(^|[\s:/])sf($|[\s:/])", text):
        return "semifinal"
    if re.search(r"\b(championship|title)\s+match\b", text) or re.search(r"\bchampionships?\b", text):
        return "final"
    if re.fullmatch(r"(the |grand )?finals?", text):
        return "final"
    if re.search(r"\b(atp|wta|nitto)\s+finals\b", text):
        return None
    if re.search(r"\bfinal\b", text) and not re.search(r"\bquarter\s*final", text):
        return "final"
    if re.search(r"\bround of (16|32|64|128)\b", text) or re.search(r"\br(16|32|64|128)\b", text):
        return "earlier"
    if re.search(r"\b(round robin|group|qualification|qualifying|first round|second round)\b", text):
        return "earlier"
    return None


def cup_round_kind(cup_round_type) -> Optional[str]:
    try:
        if cup_round_type is None or cup_round_type == "":
            return None
        return _CUP_ROUND_KIND.get(int(cup_round_type))
    except (TypeError, ValueError):
        return None


def detect_round(
    round_name: str = "",
    round_slug: str = "",
    cup_round_type=None,
    extra_labels: Optional[List[str]] = None,
) -> str:
    """Return semifinal, final, quarterfinal, earlier, or unknown.

    A recognized label or cup-round code wins. Empty metadata is unknown,
    which the strategy logs and then allows.
    """
    kinds = []
    cup_kind = cup_round_kind(cup_round_type)
    if cup_kind:
        kinds.append(cup_kind)
    for label in (round_name, round_slug, *(extra_labels or [])):
        kind = classify_round_label(label or "")
        if kind:
            kinds.append(kind)
    for kind in ("semifinal", "final", "quarterfinal", "earlier"):
        if kind in kinds:
            return kind
    return "unknown"


# Semifinal and everything later: the final and championship matches.
DEFAULT_SKIP_ROUNDS = "semifinal,final"


def parse_skip_rounds(raw: Optional[str] = None) -> set:
    """Rounds that must not be entered.

    The default is semifinal and final (semifinal and above), including a
    championship match, which is classified as a final. ``none``, ``off``, or
    an empty string disables the filter.
    """
    if raw is None:
        raw = os.environ.get("SKIP_ROUNDS", DEFAULT_SKIP_ROUNDS)
    text = (raw or "").strip().lower()
    if text in {"", "none", "off", "false", "0"}:
        return set()
    selected = set()
    for part in text.split(","):
        key = _normalize_round_text(part).replace(" ", "")
        spaced = _normalize_round_text(part)
        selected.add(_ROUND_ALIASES.get(spaced) or _ROUND_ALIASES.get(key) or key)
    return selected


@dataclass
class LiveTennisMatch:
    """A single in-progress tennis match from SofaScore."""

    home_name: str
    away_name: str
    current_set: int
    status_type: str
    status_description: str
    round_name: str = ""
    round_slug: str = ""
    cup_round_type: Optional[int] = None
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
        """Which side ``name`` is, or None when it is ambiguous.

        Checking home first is wrong when both players share a surname:
        "Yafan Wang" matches "Wang X." and "Wang Y." equally, so the home
        player would always win. In that match, given names or initials have
        to pick a unique side. If they can't, return None and the caller skips
        the match instead of trading the wrong player.
        """
        collision = players_share_surname(self.home_name, self.away_name)
        allow_surname_only = not collision
        home_hit = names_match(
            name, self.home_name, allow_surname_only=allow_surname_only
        )
        away_hit = names_match(
            name, self.away_name, allow_surname_only=allow_surname_only
        )
        if home_hit and not away_hit:
            return "home"
        if away_hit and not home_hit:
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


def _matches_from_sofascore(data) -> List[LiveTennisMatch]:
    events = data.get("events", []) if isinstance(data, dict) else []
    matches: List[LiveTennisMatch] = []
    for ev in events:
        try:
            home_team = ev.get("homeTeam") or {}
            away_team = ev.get("awayTeam") or {}
            home = home_team.get("name", "") or ""
            away = away_team.get("name", "") or ""
            status = ev.get("status") or {}
            round_info = ev.get("roundInfo") or {}
            cup_type = round_info.get("cupRoundType")
            try:
                cup_type = int(cup_type) if cup_type is not None else None
            except (TypeError, ValueError):
                cup_type = None
            if not home or not away:
                continue
            matches.append(LiveTennisMatch(
                home_name=home,
                away_name=away,
                current_set=_current_set_number(ev),
                status_type=status.get("type", ""),
                status_description=status.get("description", ""),
                round_name=str(round_info.get("name") or ""),
                round_slug=str(round_info.get("slug") or ""),
                cup_round_type=cup_type,
                home_sets_won=_sets_won(ev, "homeScore", "awayScore"),
                away_sets_won=_sets_won(ev, "awayScore", "homeScore"),
                home_ranking=_parse_ranking(home_team.get("ranking")),
                away_ranking=_parse_ranking(away_team.get("ranking")),
                home_score=ev.get("homeScore") or {},
                away_score=ev.get("awayScore") or {},
                raw=ev,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Skipping malformed SofaScore event: %s", exc)
    return matches


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


def reset_sofascore_backoff() -> None:
    """Tests start from a clean cooldown."""
    global _sofascore_retry_at, _sofascore_backoff
    _sofascore_retry_at = 0.0
    _sofascore_backoff = _SOFASCORE_BACKOFF_START


def sofascore_in_backoff() -> bool:
    return time.monotonic() < _sofascore_retry_at


def _note_sofascore_blocked() -> None:
    global _sofascore_retry_at, _sofascore_backoff
    _sofascore_retry_at = time.monotonic() + _sofascore_backoff
    _sofascore_backoff = min(_sofascore_backoff * 2, _SOFASCORE_BACKOFF_MAX)


def _note_sofascore_ok() -> None:
    reset_sofascore_backoff()


def sofascore_proxy_url() -> str:
    """Optional HTTP proxy for SofaScore only. Never logged (it may hold a password)."""
    return os.environ.get("SOFASCORE_PROXY_URL", "").strip()


def entry_matches(matches: Optional[List[LiveTennisMatch]], *, blocked: bool) -> List[LiveTennisMatch]:
    """Matches that may be used for new entries.

    A failed score fetch must not fall back to a list from an earlier cycle.
    """
    if blocked:
        return []
    return list(matches or [])


def _public_error(exc: BaseException) -> str:
    text = str(exc)
    if "@" in text or "proxy" in text.lower() or "SOFASCORE_PROXY" in text:
        return type(exc).__name__
    return text[:300]


class SofaScoreClient:
    """Minimal async client for SofaScore live tennis data (via curl_cffi)."""

    def __init__(self, timeout: float = 15.0, session: Optional[AsyncSession] = None):
        self.timeout = timeout
        self._owns_session = session is None
        self.session = session or AsyncSession()
        # Set when every live-score source failed, so the loop backs off and
        # does not open trades from a missing or leftover score list.
        self.last_fetch_blocked = False
        self.score_source = ""

    async def get_live_tennis_matches(self) -> List[LiveTennisMatch]:
        """Fetch in-progress tennis matches.

        SofaScore is tried first. A 200 with no events is an empty slate, not
        a failure. A block or transport error falls through to ESPN's public
        scoreboard. If that also fails, the result is empty and
        ``last_fetch_blocked`` is set so callers skip new entries.
        """
        self.last_fetch_blocked = False
        self.score_source = ""
        flash_matches, flash_ok = await self._load_flashscore()
        if flash_ok:
            self.score_source = "flashscore"
            line = f"scores: Flashscore ({len(flash_matches)} live)"
            logger.info(line)
            print(line, flush=True)
            return flash_matches
        if sofascore_in_backoff():
            logger.info("SofaScore cooling down after a block; trying ESPN this cycle")
            matches, ok = [], False
        else:
            matches, ok = await self._load_sofascore()
        if ok:
            self.score_source = "sofascore"
            _note_sofascore_ok()
            line = f"scores: SofaScore ({len(matches)} live)"
            logger.info(line)
            print(line, flush=True)
            return matches
        espn_matches, espn_ok = await self._load_espn()
        if espn_ok:
            self.score_source = "espn"
            line = f"scores: ESPN ({len(espn_matches)} live)"
            logger.warning("Flashscore and SofaScore unavailable; %s", line)
            print(line, flush=True)
            return espn_matches
        self.last_fetch_blocked = True
        logger.error(
            "scores: none (Flashscore, SofaScore, and ESPN failed). No new entries this cycle."
        )
        print("scores: none (Flashscore, SofaScore, and ESPN failed)", flush=True)
        return []

    async def _load_flashscore(self) -> tuple:
        from src.clients.flashscore_tennis import FLASHSCORE_URLS, flashscore_headers, matches_from_flashscore_feed

        headers = flashscore_headers()
        for url in FLASHSCORE_URLS:
            try:
                resp = await self.session.get(url, headers=headers, timeout=self.timeout)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Flashscore live fetch failed: %s", _public_error(exc))
                continue
            status = getattr(resp, "status_code", None)
            if status != 200:
                logger.warning("Flashscore live fetch HTTP %s", status)
                continue
            text = getattr(resp, "text", None)
            if text is None and hasattr(resp, "content"):
                try:
                    text = (resp.content or b"").decode("utf-8", "replace")
                except Exception:
                    text = ""
            if not (text or "").strip():
                logger.warning("Flashscore live fetch returned an empty body")
                continue
            return matches_from_flashscore_feed(text), True
        return [], False

    async def _load_sofascore(self) -> tuple:
        proxy = sofascore_proxy_url()
        if proxy:
            logger.info("SofaScore request is using SOFASCORE_PROXY_URL")
        blocked_status = None
        for url in SOFASCORE_LIVE_URLS:
            try:
                resp = await self._sofascore_get(url, proxy)
            except Exception as exc:  # noqa: BLE001 - never let SofaScore kill the loop
                logger.warning("SofaScore live fetch failed: %s", _public_error(exc))
                continue
            status = getattr(resp, "status_code", None)
            if status in (403, 429, 503):
                blocked_status = status
                continue
            if status != 200:
                logger.warning("SofaScore live fetch HTTP %s", status)
                continue
            body = getattr(resp, "content", None)
            if body == b"" or body == "":
                logger.warning("SofaScore live fetch returned an empty body")
                continue
            try:
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning("SofaScore live fetch was not JSON: %s", _public_error(exc))
                continue
            return _matches_from_sofascore(data), True
        if blocked_status is not None:
            _note_sofascore_blocked()
            logger.warning(
                "SofaScore live fetch blocked or rate-limited: HTTP %s "
                "(cooling down, ESPN will be used until the next retry)",
                blocked_status,
            )
        else:
            _note_sofascore_blocked()
            logger.warning(
                "SofaScore live fetch failed on every host "
                "(cooling down, ESPN will be used until the next retry)"
            )
        return [], False

    async def _sofascore_get(self, url: str, proxy: str):
        """GET one SofaScore URL, falling back if this curl_cffi build lacks the fingerprint."""
        last_exc = None
        for impersonate in _IMPERSONATE_TARGETS:
            kwargs = {
                "headers": _DEFAULT_HEADERS,
                "impersonate": impersonate,
                "timeout": self.timeout,
            }
            if proxy:
                kwargs["proxy"] = proxy
            try:
                return await self.session.get(url, **kwargs)
            except Exception as exc:  # noqa: BLE001
                text = str(exc).lower()
                if "impersonat" in text or "unknown target" in text:
                    last_exc = exc
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("SofaScore request was not sent")

    async def _load_espn(self) -> tuple:
        from src.clients.espn_tennis import matches_from_espn_scoreboard

        collected: List[LiveTennisMatch] = []
        any_ok = False
        for url in ESPN_SCOREBOARDS:
            try:
                resp = await self.session.get(
                    url,
                    headers={"Accept": "application/json"},
                    timeout=self.timeout,
                )
                status = getattr(resp, "status_code", None)
                if status != 200:
                    logger.warning("ESPN scoreboard HTTP %s", status)
                    continue
                payload = resp.json()
                if not isinstance(payload, dict):
                    logger.warning("ESPN scoreboard returned a non-object body")
                    continue
                any_ok = True
                collected.extend(matches_from_espn_scoreboard(payload))
            except Exception as exc:  # noqa: BLE001
                logger.warning("ESPN scoreboard failed: %s", _public_error(exc))
        return collected, any_ok

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
