"""SofaScore failure falls through to ESPN, and a total failure opens no entries."""

from __future__ import annotations

import logging

import pytest

from src.clients.espn_tennis import matches_from_espn_scoreboard
from src.clients.sofascore_client import (
    SofaScoreClient,
    entry_matches,
    reset_sofascore_backoff,
)


class _Response:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, responses, flashscore_body=None, flashscore_status=404):
        self._responses = list(responses)
        self.calls = []
        self.flashscore_body = flashscore_body
        self.flashscore_status = flashscore_status

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "flashscore" in url:
            if self.flashscore_body is None and self.flashscore_status != 200:
                return _Response(self.flashscore_status, {})
            response = _Response(self.flashscore_status, {})
            response.text = self.flashscore_body or ""
            return response
        if not self._responses:
            raise RuntimeError("no scripted response")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        return None


def _sofascore_event():
    return {
        "events": [
            {
                "homeTeam": {"name": "Alcaraz C.", "ranking": 2},
                "awayTeam": {"name": "Sinner J.", "ranking": 1},
                "homeScore": {"period1": 6, "period2": 4, "period3": 3},
                "awayScore": {"period1": 4, "period2": 6, "period3": 1},
                "status": {"type": "inprogress", "description": "3rd set"},
                "roundInfo": {"name": "Quarterfinal", "slug": "quarterfinals", "cupRoundType": 4},
            }
        ]
    }


def _espn_live():
    return {
        "events": [
            {
                "name": "Some Open",
                "groupings": [
                    {
                        "competitions": [
                            {
                                "status": {
                                    "period": 3,
                                    "type": {
                                        "state": "in",
                                        "shortDetail": "3rd Set",
                                        "description": "In Progress",
                                    },
                                },
                                "round": {"displayName": "Quarterfinals"},
                                "competitors": [
                                    {
                                        "homeAway": "home",
                                        "athlete": {"displayName": "Carlos Alcaraz"},
                                        "linescores": [
                                            {"value": 6.0},
                                            {"value": 4.0},
                                            {"value": 3.0},
                                        ],
                                    },
                                    {
                                        "homeAway": "away",
                                        "athlete": {"displayName": "Jannik Sinner"},
                                        "linescores": [
                                            {"value": 4.0},
                                            {"value": 6.0},
                                            {"value": 1.0},
                                        ],
                                    },
                                ],
                            },
                            {
                                "status": {
                                    "period": 2,
                                    "type": {"state": "post", "description": "Final"},
                                },
                                "competitors": [
                                    {
                                        "homeAway": "home",
                                        "athlete": {"displayName": "Finished Player"},
                                        "linescores": [{"value": 6}, {"value": 6}],
                                    },
                                    {
                                        "homeAway": "away",
                                        "athlete": {"displayName": "Other Player"},
                                        "linescores": [{"value": 3}, {"value": 2}],
                                    },
                                ],
                            },
                        ]
                    }
                ],
            }
        ]
    }


def test_flashscore_feed_parses_live_singles_and_skips_the_rest():
    from pathlib import Path

    from src.clients.flashscore_tennis import matches_from_flashscore_feed
    from src.clients.sofascore_client import classify_round_label

    raw = Path("tests/fixtures/flashscore_live_sample.txt").read_text(encoding="utf-8")
    matches = matches_from_flashscore_feed(raw)
    names = [(m.home_name, m.away_name, m.current_set) for m in matches]
    assert ("Sureshkumar M.", "Sasikumar M.", 1) in names
    assert ("Langmo C.", "Dellavedova M.", 3) in names
    assert all("/" not in m.home_name and "/" not in m.away_name for m in matches)
    assert all(m.status_type == "inprogress" for m in matches)
    live = next(m for m in matches if m.home_name == "Sureshkumar M.")
    assert live.home_score["period1"] == 5
    assert live.away_score["period1"] == 5
    assert live.home_sets_won == 0
    assert live.is_live is True
    late = next(m for m in matches if m.home_name == "Langmo C.")
    assert late.home_score == {"period1": 6, "period2": 4, "period3": 4}
    assert late.away_score["period3"] == 2
    assert late.leading_set_by("Langmo C.", 3, 2) is True
    assert classify_round_label(late.round_name) == "semifinal"
    assert "Sweeny" not in {m.home_name for m in matches}
    assert matches_from_flashscore_feed("") == []


@pytest.mark.asyncio
async def test_flashscore_success_is_used_before_sofascore(caplog):
    from pathlib import Path

    reset_sofascore_backoff()
    caplog.set_level(logging.INFO)
    raw = Path("tests/fixtures/flashscore_live_sample.txt").read_text(encoding="utf-8")
    session = _Session([], flashscore_body=raw, flashscore_status=200)
    client = SofaScoreClient(session=session)
    matches = await client.get_live_tennis_matches()
    assert client.score_source == "flashscore"
    assert client.last_fetch_blocked is False
    assert any(m.home_name == "Sureshkumar M." for m in matches)
    assert all("sofascore" not in url for url, _ in session.calls)
    assert session.calls[0][1]["headers"]["x-fsign"] == "SW9D1eZo"
    assert "scores: Flashscore" in caplog.text


def test_espn_parser_keeps_only_in_progress_singles():
    matches = matches_from_espn_scoreboard(_espn_live())
    assert len(matches) == 1
    match = matches[0]
    assert match.home_name == "Carlos Alcaraz"
    assert match.away_name == "Jannik Sinner"
    assert match.current_set == 3
    assert match.status_type == "inprogress"
    assert match.is_live is True
    assert match.home_score["period3"] == 3
    assert match.away_score["period1"] == 4
    assert match.home_sets_won == 1
    assert match.away_sets_won == 1
    assert match.round_name == "Quarterfinals"
    assert match.leading_set_by("Carlos Alcaraz", 3, 2) is True
    assert matches_from_espn_scoreboard({"events": []}) == []


@pytest.mark.asyncio
async def test_sofascore_success_does_not_call_espn():
    reset_sofascore_backoff()
    session = _Session([_Response(200, _sofascore_event())])
    client = SofaScoreClient(session=session)
    matches = await client.get_live_tennis_matches()
    assert client.last_fetch_blocked is False
    assert client.score_source == "sofascore"
    assert len(matches) == 1
    assert matches[0].home_name == "Alcaraz C."
    sofascore_calls = [(url, kwargs) for url, kwargs in session.calls if "sofascore.com" in url or "sofascore.app" in url]
    assert len(sofascore_calls) == 1
    url, kwargs = sofascore_calls[0]
    assert "sofascore" in url
    assert kwargs["impersonate"] == "chrome152"
    assert kwargs["headers"]["Origin"] == "https://www.sofascore.com"
    assert kwargs["headers"]["Referer"] == "https://www.sofascore.com/"
    assert "Accept-Language" in kwargs["headers"]
    assert "User-Agent" not in kwargs["headers"]
    assert "proxy" not in kwargs


@pytest.mark.asyncio
async def test_sofascore_403_uses_espn_and_proxy(monkeypatch):
    reset_sofascore_backoff()
    monkeypatch.setenv("SOFASCORE_PROXY_URL", "http://user:secret@proxy.example:8080")
    session = _Session([
        _Response(403, {"error": "challenge"}),
        _Response(403, {"error": "challenge"}),
        _Response(403, {"error": "challenge"}),
        _Response(200, _espn_live()),
        _Response(200, {"events": []}),
    ])
    client = SofaScoreClient(session=session)
    matches = await client.get_live_tennis_matches()
    assert client.last_fetch_blocked is False
    assert client.score_source == "espn"
    assert [m.home_name for m in matches] == ["Carlos Alcaraz"]
    sofascore_calls = [(url, kwargs) for url, kwargs in session.calls if "sofascore" in url]
    espn_calls = [(url, kwargs) for url, kwargs in session.calls if "sofascore" not in url]
    assert len(sofascore_calls) == 3
    assert all(kwargs["proxy"] == "http://user:secret@proxy.example:8080" for _, kwargs in sofascore_calls)
    assert espn_calls
    assert all("proxy" not in kwargs for _, kwargs in espn_calls)


@pytest.mark.asyncio
async def test_sofascore_403_backs_off_and_keeps_using_espn(caplog):
    reset_sofascore_backoff()
    caplog.set_level(logging.INFO)
    session = _Session([
        _Response(403, {"error": "challenge"}),
        _Response(403, {"error": "challenge"}),
        _Response(403, {"error": "challenge"}),
        _Response(200, _espn_live()),
        _Response(200, {"events": []}),
        _Response(200, _espn_live()),
        _Response(200, {"events": []}),
    ])
    client = SofaScoreClient(session=session)
    first = await client.get_live_tennis_matches()
    assert client.score_source == "espn"
    assert len(first) == 1
    sofascore_calls = [url for url, _ in session.calls if "sofascore" in url]
    assert len(sofascore_calls) == 3
    used = len(session.calls)
    second = await client.get_live_tennis_matches()
    assert [m.home_name for m in second] == ["Carlos Alcaraz"]
    assert client.score_source == "espn"
    assert all("sofascore" not in url for url, _ in session.calls[used:])
    assert "scores: ESPN (1 live)" in caplog.text
    reset_sofascore_backoff()


@pytest.mark.asyncio
async def test_both_score_sources_fail_blocks_entries(caplog):
    reset_sofascore_backoff()
    caplog.set_level(logging.WARNING)
    session = _Session([
        _Response(403, {}),
        RuntimeError("proxy auth failed user:secret@proxy"),
        _Response(503, {}),
    ])
    client = SofaScoreClient(session=session)
    matches = await client.get_live_tennis_matches()
    assert matches == []
    assert client.last_fetch_blocked is True
    assert client.score_source == ""
    assert "Flashscore, SofaScore, and ESPN failed" in caplog.text
    assert "secret" not in caplog.text
    assert entry_matches(
        [matches_from_espn_scoreboard(_espn_live())[0]],
        blocked=True,
    ) == []
