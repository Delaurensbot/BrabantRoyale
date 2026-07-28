import unittest

from scouting_metrics import (
    build_fit_payload,
    build_profile_metrics,
    build_war_metrics,
)


class ScoutingMetricsTests(unittest.TestCase):
    def test_normalizes_rarity_relative_card_levels(self):
        player = {
            "cards": [
                {"level": 16, "maxLevel": 16},
                {"level": 14, "maxLevel": 14},
                {"level": 13, "maxLevel": 16},
            ],
            "bestTrophies": 9000,
            "challengeMaxWins": 12,
        }

        profile = build_profile_metrics(player)

        self.assertEqual(profile["detected_max_card_level"], 16)
        self.assertEqual(profile["cards_level_16"], 2)
        self.assertEqual(profile["cards_level_15_plus"], 2)

    def test_war_metrics_use_only_played_rows(self):
        rows = [
            {
                "race_created_at": "2026-07-13T09:42:06Z",
                "contribution": 2600,
                "decks_used": 16,
            },
            {
                "race_created_at": "2026-07-20T09:42:06Z",
                "contribution": 0,
                "decks_used": 0,
            },
            {
                "race_created_at": "2026-07-27T09:43:01Z",
                "contribution": 2800,
                "decks_used": 15,
            },
        ]

        war = build_war_metrics(rows)

        self.assertEqual(war["weeks_observed"], 3)
        self.assertEqual(war["weeks_played"], 2)
        self.assertEqual(war["missed_attacks"], 1)
        self.assertEqual(war["average_contribution"], 2700)

    def test_external_player_never_gets_high_confidence_without_war_sample(self):
        payload = build_fit_payload(
            {
                "tag": "#PLAYER",
                "name": "Alice",
                "clan": {"tag": "#OTHER", "name": "Other"},
                "cards": [{"level": 16, "maxLevel": 16}] * 60,
                "bestTrophies": 9000,
                "challengeMaxWins": 12,
            },
            [],
            clan_tag="9YP8UY",
        )

        self.assertEqual(payload["mode"], "extern")
        self.assertEqual(payload["fit"]["confidence"], "laag")
        self.assertIn("proefperiode", payload["fit"]["label"].lower())

    def test_reliable_lower_output_is_not_labeled_high_risk(self):
        payload = build_fit_payload(
            {
                "tag": "#PLAYER",
                "name": "Alice",
                "clan": {"tag": "#9YP8UY", "name": "Brabant Royale"},
                "cards": [{"level": 15, "maxLevel": 16}] * 40,
                "bestTrophies": 8500,
                "challengeMaxWins": 10,
            },
            [
                {
                    "race_created_at": f"2026-0{month}-20T09:42:06Z",
                    "contribution": 2300,
                    "decks_used": 16,
                }
                for month in range(1, 7)
            ],
            clan_tag="9YP8UY",
        )

        self.assertEqual(
            payload["fit"]["label"], "Betrouwbaar, lagere war-output"
        )


if __name__ == "__main__":
    unittest.main()
