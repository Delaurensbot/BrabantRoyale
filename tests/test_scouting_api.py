import unittest
from unittest.mock import patch

from api.scouting import collect_scouting_payload


class ScoutingApiTests(unittest.TestCase):
    @patch("api.scouting.fetch_week_exclusions")
    @patch("api.scouting.fetch_history_rows")
    @patch("api.scouting.fetch_player")
    def test_excluded_war_week_is_removed_from_fit_sample(
        self,
        mocked_player,
        mocked_history,
        mocked_exclusions,
    ):
        mocked_player.return_value = {
            "tag": "#PLAYER1",
            "name": "Alice",
            "clan": {"tag": "#9YP8UY", "name": "Brabant Royale"},
            "cards": [{"level": 16, "maxLevel": 16}] * 20,
            "bestTrophies": 8500,
        }
        mocked_history.return_value = [
            {
                "race_created_at": "2026-07-20T09:42:06Z",
                "clan_tag": "9YP8UY",
                "player_tag": "PLAYER1",
                "contribution": 1000,
                "decks_used": 7,
            },
            {
                "race_created_at": "2026-07-27T09:43:01Z",
                "clan_tag": "9YP8UY",
                "player_tag": "PLAYER1",
                "contribution": 2600,
                "decks_used": 16,
            },
        ]
        mocked_exclusions.return_value = [
            {
                "race_created_at": "2026-07-20T09:42:06Z",
                "clan_tag": "9YP8UY",
                "player_tag": "PLAYER1",
            }
        ]

        with patch.dict(
            "os.environ",
            {"CLASH_ROYALE_API_KEY": "test-key"},
            clear=False,
        ):
            payload = collect_scouting_payload("PLAYER1", "9YP8UY")

        self.assertEqual(payload["excluded_weeks"], 1)
        self.assertEqual(payload["war"]["weeks_played"], 1)
        self.assertEqual(payload["war"]["missed_attacks"], 0)


if __name__ == "__main__":
    unittest.main()

