import unittest
from unittest.mock import Mock, patch

from supabase_history import (
    _supabase_headers,
    build_snapshot_rows,
    fetch_history_rows,
    fetch_week_exclusions,
    history_rows_to_races,
    upsert_snapshot_rows,
)


class SupabaseHistoryTests(unittest.TestCase):
    def test_new_api_keys_are_not_sent_as_bearer_tokens(self):
        headers = _supabase_headers(
            "sb_publishable_example",
            write=True,
            ingest_token="scheduled-token",
        )

        self.assertEqual(headers["apikey"], "sb_publishable_example")
        self.assertNotIn("Authorization", headers)
        self.assertIn("resolution=merge-duplicates", headers["Prefer"])
        self.assertEqual(headers["X-Ingest-Token"], "scheduled-token")

    def test_legacy_api_keys_are_sent_as_bearer_tokens(self):
        headers = _supabase_headers("legacy.jwt.value")

        self.assertEqual(headers["Authorization"], "Bearer legacy.jwt.value")

    def test_build_snapshot_rows_normalizes_and_dedupes_players(self):
        members = [{"tag": "#PLAYER1", "name": "Alice", "role": "elder"}]
        races = [
            {
                "seasonId": 128,
                "createdDate": "20250101T120000.000Z",
                "standings": [
                    {
                        "clan": {
                            "tag": "#9YP8UY",
                            "participants": [
                                {
                                    "tag": "#PLAYER1",
                                    "name": "Alice",
                                    "fame": 2500,
                                    "repairPoints": 100,
                                    "decksUsed": 18,
                                }
                            ],
                        }
                    }
                ],
            }
        ]

        rows = build_snapshot_rows("9yp8uy", "Brabant Royale", members, races)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["clan_tag"], "9YP8UY")
        self.assertEqual(rows[0]["player_role"], "elder")
        self.assertEqual(rows[0]["contribution"], 2600)
        self.assertEqual(rows[0]["decks_used"], 16)
        self.assertEqual(rows[0]["race_created_at"], "2025-01-01T12:00:00Z")

    def test_history_rows_rebuild_race_shape(self):
        rows = [
            {
                "season_id": 128,
                "race_created_at": "2025-01-01T12:00:00+00:00",
                "player_tag": "PLAYER1",
                "player_name": "Alice",
                "player_role": "elder",
                "fame": 2500,
                "repair_points": 100,
                "decks_used": 16,
            }
        ]

        races = history_rows_to_races(rows, "9YP8UY")

        self.assertEqual(len(races), 1)
        self.assertEqual(races[0]["createdDate"], "20250101T120000.000Z")
        participant = races[0]["clans"][0]["participants"][0]
        self.assertEqual(participant["name"], "Alice")
        self.assertEqual(participant["repairPoints"], 100)

    @patch("supabase_history.requests.get")
    def test_history_reads_are_paginated(self, mocked_get):
        first = Mock(status_code=206, content=b"[]")
        first.json.return_value = [{"player_tag": "A"}, {"player_tag": "B"}]
        second = Mock(status_code=200, content=b"[]")
        second.json.return_value = [{"player_tag": "C"}]
        mocked_get.side_effect = [first, second]

        rows = fetch_history_rows(
            "9YP8UY",
            supabase_url="https://example.supabase.co",
            api_key="sb_publishable_example",
            page_size=2,
        )

        self.assertEqual([row["player_tag"] for row in rows], ["A", "B", "C"])
        self.assertEqual(
            mocked_get.call_args_list[0].kwargs["headers"]["Range"],
            "0-1",
        )
        self.assertEqual(
            mocked_get.call_args_list[1].kwargs["headers"]["Range"],
            "2-3",
        )

    @patch("supabase_history.requests.get")
    def test_week_exclusion_read_filters_by_clan_and_player(self, mocked_get):
        response = Mock(status_code=200, content=b"[]")
        response.json.return_value = [{"reason": "Afwezig"}]
        mocked_get.return_value = response

        rows = fetch_week_exclusions(
            "9YP8UY",
            player_tag="#PLAYER1",
            supabase_url="https://example.supabase.co",
            api_key="sb_publishable_example",
        )

        self.assertEqual(rows[0]["reason"], "Afwezig")
        params = mocked_get.call_args.kwargs["params"]
        self.assertEqual(params["clan_tag"], "eq.9YP8UY")
        self.assertEqual(params["player_tag"], "eq.PLAYER1")

    @patch("supabase_history.requests.post")
    def test_snapshot_upsert_uses_natural_conflict_key(self, mocked_post):
        mocked_post.return_value = Mock(status_code=201)

        written = upsert_snapshot_rows(
            [{"clan_tag": "9YP8UY"}],
            supabase_url="https://example.supabase.co",
            api_key="sb_secret_example",
        )

        self.assertEqual(written, 1)
        url = mocked_post.call_args.args[0]
        self.assertIn(
            "on_conflict=clan_tag,race_created_at,player_tag",
            url,
        )


if __name__ == "__main__":
    unittest.main()
