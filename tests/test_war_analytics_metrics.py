import unittest
from unittest.mock import Mock, patch

from war_analytics_metrics import collect_analytics_data, dedupe_and_label_races


class DedupeAndLabelRacesTests(unittest.TestCase):
    def test_assigns_logical_week_numbers_per_season(self):
        race_items = [
            {"seasonId": 128, "createdDate": "20250101T120000.000Z"},
            {"seasonId": 128, "createdDate": "20250108T120000.000Z"},
            {"seasonId": 128, "createdDate": "20250115T120000.000Z"},
            {"seasonId": 129, "createdDate": "20250122T120000.000Z"},
            {"seasonId": 129, "createdDate": "20250129T120000.000Z"},
            {"seasonId": 129, "createdDate": "20250205T120000.000Z"},
        ]

        labeled = dedupe_and_label_races(race_items)

        self.assertEqual(
            [week_key for week_key, _ in labeled],
            ["128-1", "128-2", "128-3", "129-1", "129-2", "129-3"],
        )

    def test_dedupes_overlapping_log_and_current_race_snapshots(self):
        race_items = [
            {"seasonId": 128, "createdDate": "20250101T120000.000Z"},
            {"seasonId": 128, "createdDate": "20250101T120000.000Z", "is_current": True},
            {"seasonId": 128, "createdDate": "20250108T120000.000Z"},
            {"seasonId": 130, "createdDate": "20250226T120000.000Z"},
        ]

        labeled = dedupe_and_label_races(race_items)

        self.assertEqual(
            [week_key for week_key, _ in labeled],
            ["128-1", "128-2", "130-1"],
        )

    def test_keeps_distinct_snapshots_on_the_same_calendar_day(self):
        race_items = [
            {"seasonId": 128, "createdDate": "20250101T120000.000Z"},
            {"seasonId": 128, "createdDate": "20250101T180000.000Z"},
        ]

        labeled = dedupe_and_label_races(race_items)

        self.assertEqual(
            [week_key for week_key, _ in labeled],
            ["128-1", "128-2"],
        )


class AnalyticsHistoryMergeTests(unittest.TestCase):
    @patch("war_analytics_metrics.load_history_races_from_env")
    @patch("war_analytics_metrics.requests.get")
    def test_collect_analytics_merges_live_and_stored_weeks(
        self,
        mocked_get,
        mocked_history,
    ):
        def response(payload):
            mocked = Mock(status_code=200, content=b"json")
            mocked.json.return_value = payload
            return mocked

        mocked_get.side_effect = [
            response(
                {
                    "items": [
                        {
                            "tag": "#PLAYER1",
                            "name": "Alice",
                            "role": "member",
                        }
                    ]
                }
            ),
            response(
                {
                    "items": [
                        {
                            "seasonId": 128,
                            "createdDate": "20250108T120000.000Z",
                            "standings": [
                                {
                                    "clan": {
                                        "tag": "#9YP8UY",
                                        "participants": [
                                            {
                                                "tag": "#PLAYER1",
                                                "name": "Alice",
                                                "fame": 2000,
                                                "repairPoints": 0,
                                                "decksUsed": 16,
                                            }
                                        ],
                                    }
                                }
                            ],
                        }
                    ]
                }
            ),
            response({}),
        ]
        mocked_history.return_value = (
            [
                {
                    "seasonId": 128,
                    "createdDate": "20250101T120000.000Z",
                    "clans": [
                        {
                            "tag": "#9YP8UY",
                            "participants": [
                                {
                                    "tag": "#PLAYER1",
                                    "name": "Alice",
                                    "fame": 1000,
                                    "repairPoints": 0,
                                    "decksUsed": 16,
                                }
                            ],
                        }
                    ],
                }
            ],
            {"enabled": True, "source": "supabase_and_clash_api"},
        )

        with patch.dict(
            "os.environ",
            {"CLASH_ROYALE_API_KEY": "test-key"},
            clear=False,
        ):
            payload = collect_analytics_data(clan_tag="9YP8UY")

        self.assertEqual(
            payload["contribution_table"]["headers"][-2:],
            ["128-1", "128-2"],
        )
        self.assertEqual(payload["history"]["available_weeks"], 2)
        self.assertEqual(payload["ratio_scores"][0]["weeks_played"], 2)


if __name__ == "__main__":
    unittest.main()
