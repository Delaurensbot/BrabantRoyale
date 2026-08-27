import json
import os
from unittest.mock import Mock, patch

import requests
import pytest

from api.discord_webhook import (
    DISCORD_WEBHOOK_URL_ENV,
    build_discord_payload,
    is_alertable_event,
    send_discord_webhook,
)
import api.war_monitor as monitor
from supabase_history import claim_notification_log, read_notification_log


WEBHOOK_URL = "https://discord.com/api/webhooks/123/test-token"
SUPABASE_URL = "https://example.supabase.co"
SUPABASE_SERVER_KEY = "sb_secret_test-key"
OBSERVED_AT = "2026-08-27T08:11:00Z"


class FakeResponse:
    def __init__(self, status_code, *, headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = b"" if payload is None else b"{}"
        self._payload = payload

    def json(self):
        return self._payload


def event(**overrides):
    value = {
        "event_key": "9YP8UY|134|3|2026-08-27T08:00:00Z|2|PLAYER1|duel_first_likely",
        "event_type": "duel_first_likely",
        "confidence": "medium",
        "observed_at": OBSERVED_AT,
        "player_tag": "PLAYER1",
        "clan_tag": "9YP8UY",
        "observed_decks_used_today": 2,
        "details": {
            "race_day_key": "9YP8UY|134|3|2026-08-27T08:00:00Z|2",
        },
    }
    value.update(overrides)
    return value


def candidate():
    return {
        "event_key": event()["event_key"],
        "channel": "discord",
        "status": "pending",
        "details": {
            "event_type": "duel_first_likely",
            "status": "duel_first_likely",
            "confidence": "medium",
            "observed_at": OBSERVED_AT,
            "race_day_key": event()["details"]["race_day_key"],
            "clan_tag": "9YP8UY",
            "clan_name": "Brabant Royale",
            "player_tag": "PLAYER1",
            "player_name": "Alice",
            "current_decks_used_today": 2,
        },
    }


def test_payload_is_allow_listed_and_contains_no_webhook_or_database_secret():
    database_secret = "database-secret-used-only-by-server"
    payload = build_discord_payload(
        event(details={"race_day_key": "race-1", "secret": database_secret}),
        clan_name="Brabant Royale",
        player_name="Alice",
    )

    assert set(payload) == {"content", "allowed_mentions"}
    assert payload["allowed_mentions"] == {"parse": []}
    assert "Duel-eerst controle" in payload["content"]
    assert "Alice (#PLAYER1)" in payload["content"]
    assert "#9YP8UY" in payload["content"]
    encoded = json.dumps(payload, ensure_ascii=False)
    assert WEBHOOK_URL not in encoded
    assert database_secret not in encoded
    assert "Authorization" not in encoded
    assert "Bearer" not in encoded


def test_webhook_url_and_upstream_exception_text_are_redacted_from_result():
    post = Mock(side_effect=RuntimeError(f"upstream leaked {WEBHOOK_URL}"))

    result = send_discord_webhook(
        build_discord_payload(event(), clan_name="Brabant Royale", player_name="Alice"),
        webhook_url=WEBHOOK_URL,
        http_post=post,
        max_retries=0,
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["attempts"] == 1
    assert result["error"] == "transport_error"
    assert result["sent_at"]
    assert WEBHOOK_URL not in repr(result)
    assert "upstream leaked" not in repr(result)


@pytest.mark.parametrize("status_code", [200, 201, 204])
def test_2xx_and_204_are_success(status_code):
    post = Mock(return_value=FakeResponse(status_code))
    sleep = Mock()

    result = send_discord_webhook(
        build_discord_payload(event(), clan_name="Brabant Royale", player_name="Alice"),
        webhook_url=WEBHOOK_URL,
        http_post=post,
        sleep=sleep,
        clock=lambda: OBSERVED_AT,
    )

    assert result["ok"] is True
    assert result["status"] == "sent"
    assert result["response_code"] == status_code
    assert result["sent_at"] == OBSERVED_AT
    assert result["attempts"] == 1
    assert sleep.call_count == 0
    request_kwargs = post.call_args.kwargs
    assert request_kwargs["json"]["allowed_mentions"] == {"parse": []}
    assert "Authorization" not in request_kwargs["headers"]
    assert "Bearer" not in repr(request_kwargs["headers"])


def test_400_is_permanent_and_is_not_retried():
    post = Mock(return_value=FakeResponse(400))
    sleep = Mock()

    result = send_discord_webhook(
        build_discord_payload(event(), clan_name="Brabant Royale", player_name="Alice"),
        webhook_url=WEBHOOK_URL,
        http_post=post,
        max_retries=3,
        sleep=sleep,
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error"] == "bad_request"
    assert result["response_code"] == 400
    assert result["attempts"] == 1
    post.assert_called_once()
    sleep.assert_not_called()


def test_429_honors_retry_after_with_a_bounded_delay():
    post = Mock(
        side_effect=[
            FakeResponse(429, headers={"Retry-After": "999"}),
            FakeResponse(429, headers={"Retry-After": "999"}),
            FakeResponse(204),
        ]
    )
    sleep = Mock()

    result = send_discord_webhook(
        build_discord_payload(event(), clan_name="Brabant Royale", player_name="Alice"),
        webhook_url=WEBHOOK_URL,
        http_post=post,
        max_retries=2,
        backoff_factor=0.1,
        max_backoff=0.25,
        sleep=sleep,
    )

    assert result["status"] == "sent"
    assert result["attempts"] == 3
    assert sleep.call_args_list == [((0.25,), {}), ((0.25,), {})]


def test_5xx_and_timeout_have_bounded_retries_and_safe_final_result():
    post = Mock(
        side_effect=[
            FakeResponse(503),
            requests.exceptions.Timeout(f"timeout leaked {WEBHOOK_URL}"),
            FakeResponse(204),
        ]
    )
    sleep = Mock()

    result = send_discord_webhook(
        build_discord_payload(event(), clan_name="Brabant Royale", player_name="Alice"),
        webhook_url=WEBHOOK_URL,
        http_post=post,
        max_retries=2,
        backoff_factor=0.1,
        max_backoff=0.2,
        sleep=sleep,
    )

    assert result["status"] == "sent"
    assert result["attempts"] == 3
    assert sleep.call_count == 2
    assert WEBHOOK_URL not in repr(result)

    post.reset_mock()
    post.side_effect = [requests.exceptions.Timeout(WEBHOOK_URL)] * 2
    final = send_discord_webhook(
        build_discord_payload(event(), clan_name="Brabant Royale", player_name="Alice"),
        webhook_url=WEBHOOK_URL,
        http_post=post,
        max_retries=1,
        backoff_factor=0,
        sleep=sleep,
    )
    assert final["status"] == "failed"
    assert final["error"] == "timeout"
    assert final["attempts"] == 2
    assert WEBHOOK_URL not in repr(final)


@pytest.mark.parametrize("status", ["unknown_start", "api_stale", "exempt"])
def test_non_alertable_t07_statuses_are_filtered(status):
    suppressed = event(event_type=status, observed_decks_used_today=None)
    assert is_alertable_event(suppressed) is False
    with pytest.raises(ValueError):
        build_discord_payload(suppressed)


def test_alertable_event_without_counter_is_filtered():
    assert is_alertable_event(event(observed_decks_used_today=None)) is False


class DurableNotificationStore:
    def __init__(self):
        self.rows = {}
        self.reads = []
        self.writes = []

    def read_notification_log(self, event_key, channel):
        self.reads.append((event_key, channel))
        row = self.rows.get((event_key, channel))
        return {
            "ok": True,
            "status": "fresh" if row else "empty",
            "notification_log": dict(row) if row else None,
        }

    def claim_notification_log(self, entry):
        key = (entry["event_key"], entry["channel"])
        if key in self.rows:
            return {"ok": True, "status": "exists", "claimed": False}
        self.rows[key] = dict(entry)
        return {"ok": True, "status": "claimed", "claimed": True}

    def write_notification_logs(self, entries):
        entries = [dict(entry) for entry in entries]
        self.writes.extend(entries)
        for entry in entries:
            key = (entry["event_key"], entry["channel"])
            self.rows[key] = entry
        return {"ok": True, "status": "ok", "rows_written": len(entries)}


def test_durable_notification_claim_prevents_duplicate_sends():
    store = DurableNotificationStore()
    functions = monitor.MonitorFunctions(
        write_live_snapshots=Mock(),
        read_previous_player_snapshot=Mock(),
        read_day_event=None,
        write_day_events=Mock(),
        write_notification_logs=store.write_notification_logs,
        read_notification_log=store.read_notification_log,
        claim_notification_log=store.claim_notification_log,
    )
    post = Mock(return_value=FakeResponse(204))

    first = monitor._dispatch_discord_notifications(
        functions,
        [candidate()],
        WEBHOOK_URL,
        discord_post_fn=post,
        discord_clock_fn=lambda: OBSERVED_AT,
    )
    second = monitor._dispatch_discord_notifications(
        functions,
        [candidate()],
        WEBHOOK_URL,
        discord_post_fn=post,
        discord_clock_fn=lambda: OBSERVED_AT,
    )

    assert first["sent"] == 1
    assert second["skipped"] == 1
    assert second["failed"] == 0
    post.assert_called_once()
    assert store.rows[(candidate()["event_key"], "discord")]["status"] == "sent"
    assert len(store.reads) == 2


def test_missing_webhook_configuration_is_explicitly_disabled_and_does_not_post():
    post = Mock()
    payload = build_discord_payload(event(), clan_name="Brabant Royale", player_name="Alice")
    with patch.dict(os.environ, {}, clear=True):
        result = send_discord_webhook(payload, http_post=post)

    assert result == {"ok": True, "status": "disabled", "attempts": 0}
    post.assert_not_called()


def test_invalid_webhook_configuration_is_disabled_without_returning_the_url():
    invalid_url = "http://discord.com/api/webhooks/not-used"
    with patch.dict(os.environ, {DISCORD_WEBHOOK_URL_ENV: invalid_url}, clear=True):
        result = send_discord_webhook(
            build_discord_payload(event(), clan_name="Brabant Royale", player_name="Alice")
        )

    assert result == {"ok": True, "status": "disabled", "attempts": 0}
    assert invalid_url not in repr(result)


@pytest.mark.parametrize(
    "invalid_url",
    [
        "https://example.com/api/webhooks/123/token",
        "https://discord.com/not-a-webhook/123/token",
    ],
)
def test_webhook_configuration_is_limited_to_discord_webhook_hosts(invalid_url):
    with patch.dict(os.environ, {DISCORD_WEBHOOK_URL_ENV: invalid_url}, clear=True):
        result = send_discord_webhook(
            build_discord_payload(event(), clan_name="Brabant Royale", player_name="Alice")
        )

    assert result == {"ok": True, "status": "disabled", "attempts": 0}
    assert invalid_url not in repr(result)


@patch("supabase_history.requests.get")
def test_notification_log_read_is_server_only_and_uses_event_channel_key(mocked_get):
    mocked_get.return_value = FakeResponse(
        200,
        payload=[
            {
                "event_key": event()["event_key"],
                "channel": "discord",
                "status": "sent",
                "response_code": 204,
                "sent_at": OBSERVED_AT,
                "details": {},
            }
        ],
    )

    result = read_notification_log(
        event()["event_key"],
        "discord",
        supabase_url=SUPABASE_URL,
        api_key=SUPABASE_SERVER_KEY,
    )

    assert result["status"] == "fresh"
    assert result["notification_log"]["status"] == "sent"
    params = mocked_get.call_args.kwargs["params"]
    assert params["event_key"] == f"eq.{event()['event_key']}"
    assert params["channel"] == "eq.discord"
    assert "Authorization" not in mocked_get.call_args.kwargs["headers"]


@patch("supabase_history.requests.post")
def test_notification_log_claim_is_atomic_and_does_not_return_response_body(mocked_post):
    mocked_post.return_value = FakeResponse(
        201,
        payload=[
            {
                "event_key": event()["event_key"],
                "channel": "discord",
                "status": "pending",
            }
        ],
    )

    result = claim_notification_log(
        candidate(),
        supabase_url=SUPABASE_URL,
        api_key=SUPABASE_SERVER_KEY,
    )

    assert result["claimed"] is True
    assert result["status"] == "claimed"
    assert "notification_log" not in result
    sent = mocked_post.call_args.kwargs
    assert sent["json"][0]["status"] == "pending"
    assert sent["headers"]["Prefer"] == "resolution=ignore-duplicates,return=representation"
    assert "Authorization" not in sent["headers"]

    mocked_post.return_value = FakeResponse(201, payload=[])
    duplicate = claim_notification_log(
        candidate(),
        supabase_url=SUPABASE_URL,
        api_key=SUPABASE_SERVER_KEY,
    )
    assert duplicate["claimed"] is False
    assert duplicate["status"] == "exists"
