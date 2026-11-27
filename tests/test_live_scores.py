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
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
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
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
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
    assert "both failed" in caplog.text
    assert "secret" not in caplog.text
    assert entry_matches(
        [matches_from_espn_scoreboard(_espn_live())[0]],
        blocked=True,
    ) == []
