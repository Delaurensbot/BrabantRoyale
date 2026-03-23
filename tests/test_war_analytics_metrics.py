import unittest

from war_analytics_metrics import dedupe_and_label_races


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


if __name__ == "__main__":
    unittest.main()
