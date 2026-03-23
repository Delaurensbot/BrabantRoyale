import unittest

from war_analytics_metrics import build_player_history_summary, dedupe_and_label_races


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


class PlayerHistorySummaryTests(unittest.TestCase):
    def test_builds_full_history_metrics(self):
        summary = build_player_history_summary(
            ["128-1", "128-2", "128-3", "129-1", "129-2"],
            {
                "128-1": 2800,
                "128-2": 1900,
                "128-3": 2500,
                "129-1": 2400,
                "129-2": 2050,
            },
            {
                "128-1": 16,
                "128-2": 11,
                "128-3": 16,
                "129-1": 16,
                "129-2": 15,
            },
        )

        self.assertEqual(summary["weeks_in_history"], 5)
        self.assertEqual(summary["perfect_weeks"], 3)
        self.assertEqual(summary["longest_perfect_streak"], 2)
        self.assertEqual(summary["average_perfect_score"], 2566.67)
        self.assertEqual(summary["missed_weeks"], 2)
        self.assertEqual(summary["average_missed_attacks"], 3.0)
        self.assertEqual(summary["average_missed_score"], 1975.0)
        self.assertEqual(summary["average_score"], 2330.0)
        self.assertEqual(summary["first_week"], "128-1")
        self.assertEqual(summary["last_week"], "129-2")


if __name__ == "__main__":
    unittest.main()
