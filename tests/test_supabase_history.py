import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

from supabase_history import (
    _supabase_headers,
    build_snapshot_rows,
    fetch_history_rows,
    fetch_week_exclusions,
    get_supabase_server_config,
    history_rows_to_races,
    read_day_event,
    read_previous_player_snapshot,
    upsert_snapshot_rows,
    write_day_event,
    write_live_snapshot,
    write_live_snapshots,
    write_notification_log,
)


SUPABASE_URL = "https://example.supabase.co"
SERVER_KEY = "sb_secret_test-key"


def response(status_code, payload=None, *, content=True, headers=None):
    mocked = Mock(status_code=status_code)
    mocked.content = b"{}" if content else b""
    if payload is not None:
        mocked.json.return_value = payload
    mocked.headers = headers or {}
    return mocked


def live_snapshot_payload(**overrides):
    payload = {
        "clanTag": "#9yp8uy",
        "seasonId": 128,
        "sectionIndex": 1,
        "periodIndex": 2,
        "periodType": "war_day",
        "raceCreatedAt": "20250101T120000.000Z",
        "playerTag": "#player1",
        "playerName": "Alice",
        "playerRole": "elder",
        "decksUsed": 4,
        "decksUsedToday": 2,
        "fame": 2500,
        "repairPoints": 100,
        "boatAttacks": 1,
        "boatAttacksToday": 1,
        "boatDefenses": 0,
        "boatDefensesToday": 0,
        "capturedAt": "2025-01-01T12:05:00+00:00",
        "source": "official_api",
        "payloadVersion": 1,
    }
    payload.update(overrides)
    return payload


def day_event_payload(**overrides):
    payload = {
        "clan_tag": "#9yp8uy",
        "race_created_at": "20250101T120000.000Z",
        "period_index": 2,
        "player_tag": "#player1",
        "event_type": "decks_changed",
        "observed_decks_used_today": 2,
        "confidence": "high",
        "observed_at": "2025-01-01T12:05:00Z",
        "details": {"reason": "match"},
    }
    payload.update(overrides)
    return payload


def notification_payload(**overrides):
    payload = {
        "event_key": "9YP8UY:PLAYER1:decks_changed",
        "channel": "discord",
        "status": "sent",
        "response_code": 204,
        "sent_at": "2025-01-01T12:06:00Z",
        "details": {"message_id": "m-1"},
    }
    payload.update(overrides)
    return payload


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

    @patch("supabase_history.requests.post")
    def test_live_snapshot_maps_t05_columns_and_uses_capture_conflict_key(
        self,
        mocked_post,
    ):
        mocked_post.return_value = response(201, content=False)

        result = write_live_snapshot(
            live_snapshot_payload(extra_payload="must not be stored"),
            supabase_url=SUPABASE_URL,
            api_key=SERVER_KEY,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["rows_written"], 1)
        sent = mocked_post.call_args.kwargs["json"][0]
        self.assertEqual(sent["clan_tag"], "9YP8UY")
        self.assertEqual(sent["player_tag"], "PLAYER1")
        self.assertEqual(sent["race_created_at"], "2025-01-01T12:00:00Z")
        self.assertEqual(sent["captured_at"], "2025-01-01T12:05:00Z")
        self.assertIsNone(sent.get("extra_payload"))
        self.assertNotIn("id", sent)
        self.assertNotIn("capture_bucket", sent)
        self.assertIn(
            "on_conflict=clan_tag,race_created_at,period_index,player_tag,capture_bucket",
            mocked_post.call_args.args[0],
        )
        self.assertNotIn("Authorization", mocked_post.call_args.kwargs["headers"])

    @patch("supabase_history.requests.post")
    def test_live_snapshot_batch_is_chunked_and_preserves_null_metrics(
        self,
        mocked_post,
    ):
        mocked_post.side_effect = [
            response(201, content=False),
            response(201, content=False),
        ]
        rows = [
            live_snapshot_payload(playerTag="#player1", fame=None),
            live_snapshot_payload(playerTag="#player2"),
            live_snapshot_payload(playerTag="#player3"),
        ]

        result = write_live_snapshots(
            rows,
            supabase_url=SUPABASE_URL,
            api_key=SERVER_KEY,
            batch_size=2,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["rows_written"], 3)
        self.assertEqual(result["batches"], 2)
        self.assertEqual(len(mocked_post.call_args_list[0].kwargs["json"]), 2)
        self.assertEqual(len(mocked_post.call_args_list[1].kwargs["json"]), 1)
        self.assertIsNone(mocked_post.call_args_list[0].kwargs["json"][0]["fame"])

    @patch("supabase_history.requests.post")
    def test_day_event_upsert_maps_payload_and_uses_event_conflict_key(
        self,
        mocked_post,
    ):
        mocked_post.return_value = response(201, content=False)

        result = write_day_event(
            day_event_payload(),
            supabase_url=SUPABASE_URL,
            api_key=SERVER_KEY,
        )

        self.assertEqual(result["status"], "ok")
        sent = mocked_post.call_args.kwargs["json"][0]
        self.assertEqual(sent["clan_tag"], "9YP8UY")
        self.assertEqual(sent["details"], {"reason": "match"})
        self.assertEqual(
            mocked_post.call_args.args[0].split("?on_conflict=", 1)[1],
            "clan_tag,race_created_at,period_index,player_tag,event_type",
        )

    @patch("supabase_history.requests.post")
    def test_notification_upsert_is_idempotent_on_event_and_channel(
        self,
        mocked_post,
    ):
        mocked_post.return_value = response(201, content=False)

        result = write_notification_log(
            notification_payload(),
            supabase_url=SUPABASE_URL,
            api_key=SERVER_KEY,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["rows_written"], 1)
        sent = mocked_post.call_args.kwargs["json"][0]
        self.assertEqual(sent["response_code"], 204)
        self.assertEqual(sent["details"], {"message_id": "m-1"})
        self.assertIn(
            "on_conflict=event_key,channel",
            mocked_post.call_args.args[0],
        )

    @patch("supabase_history.requests.get")
    def test_previous_player_snapshot_read_is_latest_and_explicitly_fresh(
        self,
        mocked_get,
    ):
        previous = {
            "player_tag": "PLAYER1",
            "captured_at": "2025-01-01T12:05:00Z",
            "fame": None,
        }
        mocked_get.return_value = response(200, [previous])

        result = read_previous_player_snapshot(
            "#9yp8uy",
            "20250101T120000.000Z",
            2,
            "#player1",
            before_captured_at="2025-01-01T12:10:00Z",
            supabase_url=SUPABASE_URL,
            api_key=SERVER_KEY,
        )

        self.assertEqual(result["status"], "fresh")
        self.assertFalse(result["stale"])
        self.assertEqual(result["snapshot"], previous)
        params = mocked_get.call_args.kwargs["params"]
        self.assertEqual(params["clan_tag"], "eq.9YP8UY")
        self.assertEqual(params["player_tag"], "eq.PLAYER1")
        self.assertEqual(params["period_index"], "eq.2")
        self.assertEqual(params["captured_at"], "lt.2025-01-01T12:10:00Z")
        self.assertEqual(params["limit"], "1")

    @patch("supabase_history.requests.get")
    def test_day_event_read_returns_empty_without_zero_filling(self, mocked_get):
        mocked_get.return_value = response(200, [])

        result = read_day_event(
            "9YP8UY",
            "2025-01-01T12:00:00Z",
            2,
            "PLAYER1",
            "decks_changed",
            supabase_url=SUPABASE_URL,
            api_key=SERVER_KEY,
        )

        self.assertEqual(result["status"], "empty")
        self.assertIsNone(result["event"])
        self.assertNotIn("observed_decks_used_today", result)
        params = mocked_get.call_args.kwargs["params"]
        self.assertEqual(params["event_type"], "eq.decks_changed")

    @patch("supabase_history.requests.get")
    def test_day_event_read_can_mark_old_data_stale(self, mocked_get):
        old_timestamp = (
            datetime.now(timezone.utc) - timedelta(seconds=120)
        ).isoformat()
        mocked_get.return_value = response(
            200,
            [{"observed_at": old_timestamp, "event_type": "decks_changed"}],
        )

        result = read_day_event(
            "9YP8UY",
            "2025-01-01T12:00:00Z",
            2,
            "PLAYER1",
            "decks_changed",
            stale_after_seconds=60,
            supabase_url=SUPABASE_URL,
            api_key=SERVER_KEY,
        )

        self.assertEqual(result["status"], "stale")
        self.assertTrue(result["stale"])
        self.assertEqual(result["stale_reason"], "age")

    @patch("supabase_history.requests.post")
    def test_retryable_429_5xx_and_timeout_are_retried_with_bounded_backoff(
        self,
        mocked_post,
    ):
        mocked_post.side_effect = [
            response(429, content=False),
            response(503, content=False),
            response(201, content=False),
        ]
        sleep = Mock()

        result = write_live_snapshot(
            live_snapshot_payload(),
            supabase_url=SUPABASE_URL,
            api_key=SERVER_KEY,
            max_retries=2,
            backoff_factor=0.1,
            max_backoff=0.15,
            sleep=sleep,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(mocked_post.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.1, 0.15])

        mocked_post.reset_mock()
        mocked_post.side_effect = [
            requests.exceptions.Timeout("payload-secret"),
            response(201, content=False),
        ]
        timeout_result = write_live_snapshot(
            live_snapshot_payload(),
            supabase_url=SUPABASE_URL,
            api_key=SERVER_KEY,
            max_retries=1,
            backoff_factor=0,
            sleep=sleep,
        )
        self.assertEqual(timeout_result["status"], "ok")
        self.assertEqual(timeout_result["attempts"], 2)

    @patch("supabase_history.requests.post")
    def test_non_retryable_error_is_safe_and_does_not_retry(self, mocked_post):
        mocked_post.return_value = response(400, content=False)

        result = write_notification_log(
            notification_payload(details={"secret": "do-not-log"}),
            supabase_url=SUPABASE_URL,
            api_key=SERVER_KEY,
            max_retries=3,
            backoff_factor=0,
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "supabase_http_error")
        self.assertEqual(result["status_code"], 400)
        self.assertEqual(mocked_post.call_count, 1)
        self.assertNotIn("do-not-log", result["message"])
        self.assertNotIn(SUPABASE_URL, result["message"])

    @patch("supabase_history.requests.get")
    def test_temporary_read_error_is_error_not_a_zero_snapshot(self, mocked_get):
        mocked_get.return_value = response(503, content=False)

        result = read_previous_player_snapshot(
            "9YP8UY",
            "2025-01-01T12:00:00Z",
            2,
            "PLAYER1",
            supabase_url=SUPABASE_URL,
            api_key=SERVER_KEY,
            max_retries=0,
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "upstream_server_error")
        self.assertIsNone(result["snapshot"])
        self.assertNotIn("fame", result)
        self.assertNotIn("decks_used_today", result)

    def test_raw_storage_uses_only_server_key_configuration(self):
        with patch.dict(
            os.environ,
            {
                "SUPABASE_URL": SUPABASE_URL,
                "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_public",
                "SUPABASE_INGEST_TOKEN": "scheduled-token",
            },
            clear=True,
        ):
            self.assertIsNone(get_supabase_server_config())
            with patch("supabase_history.requests.post") as mocked_post:
                result = write_live_snapshot(live_snapshot_payload())

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "configuration_error")
        mocked_post.assert_not_called()

    def test_raw_storage_prefers_secret_or_service_role_over_public_key(self):
        with patch.dict(
            os.environ,
            {
                "SUPABASE_URL": SUPABASE_URL,
                "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_public",
                "SUPABASE_SECRET_KEY": SERVER_KEY,
                "SUPABASE_SERVICE_ROLE_KEY": "legacy-service-key",
            },
            clear=True,
        ):
            self.assertEqual(
                get_supabase_server_config(),
                (SUPABASE_URL, SERVER_KEY),
            )

    @patch("supabase_history.requests.post")
    def test_explicit_publishable_key_is_rejected_for_raw_writes(self, mocked_post):
        result = write_live_snapshot(
            live_snapshot_payload(),
            supabase_url=SUPABASE_URL,
            api_key="sb_publishable_not_allowed",
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "server_key_required")
        mocked_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
