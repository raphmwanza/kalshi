import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.clients.flashscore_tennis import matches_from_flashscore_feed
from src.clients.sofascore_client import SofaScoreClient


def feed(status="2", stage="19", score="BE÷4¬BF÷2"):
    return (f"SA÷2¬~ZA÷ATP Singles¬~AA÷match1¬AB÷{status}¬AC÷{stage}¬"
            f"AE÷Player A¬AF÷Player B¬BA÷6¬BB÷4¬BC÷4¬BD÷6¬{score}¬")


class FlashscoreRecords(unittest.TestCase):
    def test_live_requires_consistent_status_and_set(self):
        for status, stage in [("3", "19"), ("", "19"), ("2", "999")]:
            with self.subTest(status=status, stage=stage):
                self.assertEqual(matches_from_flashscore_feed(feed(status, stage)), [])

    def test_incomplete_or_invalid_scores_cannot_create_leads(self):
        for scores in ["BE÷4", "BF÷2", "BE÷4¬BF÷?", "BE÷4¬BF÷-1", ""]:
            with self.subTest(scores=scores):
                self.assertEqual(matches_from_flashscore_feed(feed(score=scores)), [])

    def test_explicit_zero_is_a_valid_score(self):
        match, = matches_from_flashscore_feed(feed(score="BE÷0¬BF÷0"))
        self.assertEqual((match.home_score["period3"], match.away_score["period3"]), (0, 0))


class FlashscoreFallback(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_success_body_advances_to_next_provider(self):
        for body in ["<html>Unavailable</html>", "garbage", "SA÷1¬~ZA÷Other sport¬", feed(score="BE÷4")]:
            session = AsyncMock()
            session.get.return_value = SimpleNamespace(status_code=200, text=body)
            client = SofaScoreClient(session=session)
            client._load_sofascore = AsyncMock(return_value=([], True))
            with self.subTest(body=body):
                await client.get_live_tennis_matches()
                self.assertEqual(client.score_source, "sofascore")

    async def test_valid_board_with_no_live_singles_remains_authoritative(self):
        session = AsyncMock()
        session.get.return_value = SimpleNamespace(status_code=200, text=feed("3", "3"))
        client = SofaScoreClient(session=session)
        self.assertEqual(await client.get_live_tennis_matches(), [])
        self.assertEqual(client.score_source, "flashscore")
        self.assertEqual(session.get.await_count, 1)
