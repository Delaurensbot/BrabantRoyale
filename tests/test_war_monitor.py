import json
import os
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from api.clash_client import ClashResponse, ResponseMetadata
from api.duel_first import (
    STATUS_DUEL_FIRST_LIKELY,
    STATUS_SOLO_START_OBSERVED,
)
import api.war_monitor as monitor


CLANS = {
    "9YP8UY": {"tag": "9YP8UY", "name": "Brabant Royale"},
    "GPCLVLPP": {"tag": "GPCLVLPP", "name": "Brabant Royale 2"},
    "RLQQQC99": {"tag": "RLQQQC99", "name": "Brabant Royale 3"},
}
RACE_CREATED_AT = "2026-08-27T08:00:00Z"
FIRST_CAPTURE = "2026-08-27T08:00:00Z"
SECOND_CAPTURE = "2026-08-27T08:11:00Z"


def envelope(payload, *, stale=False, status=None):
    del status
    return ClashResponse(
        data=payload,
        metadata=ResponseMetadata(
            source="test_clash",
            fetched_at=SECOND_CAPTURE,
            is_stale=stale,
            stale_reason="upstream_server_error" if stale else None,
            error_code="upstream_server_error" if stale else None,
        ),
    )


def race_payload(tag, decks_used_today, *, state="warDay", include_metrics=True):
    participant = {
        "tag": "#PLAYER1",
        "name": "Alice",
        "role": "member",
        "decksUsed": 4,
        "decksUsedToday": decks_used_today,
        "fame": 2500,
        "repairPoints": 100,
        "boatAttacks": 1,
        "boatAttacksToday": 1,
        "boatDefenses": 2,
        "boatDefensesToday": 1,
    }
    if not include_metrics:
        participant.pop("decksUsedToday")
        participant.pop("fame")
    clan = {
        "tag": f"#{tag}",
        "name": CLANS[tag]["name"],
        "participants": [participant],
        "decksUsedToday": decks_used_today,
        "fame": 2500,
        "repairPoints": 100,
    }
    return {
        "state": state,
        "seasonId": 134,
        "sectionIndex": 3,
        "periodIndex": 2,
        "periodType": "war",
        "createdDate": "20260827T080000.000Z",
        "clan": clan,
        "clans": [clan],
    }


class FakeClient:
    def __init__(self, races, *, failures=(), error_message="upstream fixture contains monitor-secret"):
        self.races = races
        self.failures = set(failures)
        self.error_message = error_message
        self.calls = []

    def _response(self, tag, method):
        self.calls.append((method, tag))
        if (method, tag) in self.failures:
            raise RuntimeError(self.error_message)
        if method == "get_clan":
            return envelope(
                {
                    "tag": f"#{tag}",
                    "name": CLANS[tag]["name"],
                    "members": 1,
                }
            )
        if method == "get_members":
            return envelope(
                {
                    "items": [
                        {"tag": "#PLAYER1", "name": "Alice", "role": "member"}
                    ]
                }
            )
        race = self.races[tag]
        return race if isinstance(race, ClashResponse) else envelope(race)

    def get_clan(self, tag):
        return self._response(tag, "get_clan")

    def get_members(self, tag):
        return self._response(tag, "get_members")

    def get_current_river_race(self, tag):
        return self._response(tag, "get_current_river_race")


class MemoryStorage:
    def __init__(self):
        self.snapshots = []
        self.events = []
        self.notifications = []
        self.snapshot_calls = []
        self.previous_calls = []
        self.event_read_calls = []
        self.event_write_calls = []
        self.notification_calls = []

    @staticmethod
    def _bucket(timestamp):
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        minute = parsed.minute - (parsed.minute % 10)
        return parsed.replace(minute=minute, second=0, microsecond=0)

    def write_live_snapshots(self, rows):
        rows = list(rows)
        self.snapshot_calls.append(rows)
        for row in rows:
            key = (
                row["clan_tag"],
                row["race_created_at"],
                row["period_index"],
                row["player_tag"],
                self._bucket(row["captured_at"]),
            )
            self.snapshots = [
                old
                for old in self.snapshots
                if (
                    old["clan_tag"],
                    old["race_created_at"],
                    old["period_index"],
                    old["player_tag"],
                    self._bucket(old["captured_at"]),
                )
                != key
            ]
            self.snapshots.append(dict(row))
        return {
            "ok": True,
            "status": "ok" if rows else "empty",
            "rows_written": len(rows),
        }

    def read_previous_player_snapshot(
        self,
        clan_tag,
        race_created_at,
        period_index,
        player_tag,
        *,
        before_captured_at=None,
    ):
        self.previous_calls.append(
            {
                "clan_tag": clan_tag,
                "race_created_at": race_created_at,
                "period_index": period_index,
                "player_tag": player_tag,
                "before_captured_at": before_captured_at,
            }
        )
        candidates = [
            row
            for row in self.snapshots
            if row["clan_tag"] == clan_tag
            and row["race_created_at"] == race_created_at
            and row["period_index"] == period_index
            and row["player_tag"] == player_tag
            and (
                before_captured_at is None
                or row["captured_at"] < before_captured_at
            )
        ]
        if not candidates:
            return {"ok": True, "status": "empty", "snapshot": None}
        return {
            "ok": True,
            "status": "fresh",
            "snapshot": max(candidates, key=lambda row: row["captured_at"]),
        }

    def read_day_event(
        self,
        clan_tag,
        race_created_at,
        period_index,
        player_tag,
        event_type,
    ):
        self.event_read_calls.append(
            (clan_tag, race_created_at, period_index, player_tag, event_type)
        )
        for event in self.events:
            if (
                event["clan_tag"] == clan_tag
                and event["race_created_at"] == race_created_at
                and event["period_index"] == period_index
                and event["player_tag"] == player_tag
                and event["event_type"] == event_type
            ):
                return {"ok": True, "status": "fresh", "event": dict(event)}
        return {"ok": True, "status": "empty", "event": None}

    def write_day_events(self, events):
        events = [dict(event) for event in events]
        self.event_write_calls.append(events)
        written = 0
        for event in events:
            key = (
                event["clan_tag"],
                event["race_created_at"],
                event["period_index"],
                event["player_tag"],
                event["event_type"],
            )
            if not any(
                (
                    old["clan_tag"],
                    old["race_created_at"],
                    old["period_index"],
                    old["player_tag"],
                    old["event_type"],
                )
                == key
                for old in self.events
            ):
                self.events.append(event)
                written += 1
        return {"ok": True, "status": "ok", "rows_written": written}

    def write_notification_logs(self, entries):
        entries = [dict(entry) for entry in entries]
        self.notification_calls.append(entries)
        written = 0
        for entry in entries:
            key = (entry["event_key"], entry["channel"])
            if not any((old["event_key"], old["channel"]) == key for old in self.notifications):
                self.notifications.append(entry)
                written += 1
        return {"ok": True, "status": "ok", "rows_written": written}


def run_monitor(client, storage, *, timestamp=FIRST_CAPTURE, policy=None):
    return monitor.run_war_monitor(
        client=client,
        storage=storage,
        clan_configs=CLANS,
        observed_at=timestamp,
        notification_policy=policy,
    )


def test_secret_rejection_happens_before_any_run_or_storage_call():
    run = Mock()
    with patch.dict(os.environ, {}, clear=True), patch.object(monitor, "run_war_monitor", run):
        missing = type("Request", (), {"headers": {}, "_send_json": lambda self, status, payload: setattr(self, "sent", (status, payload))})()
        monitor.handler.do_POST(missing)

    assert missing.sent[0] == 500
    run.assert_not_called()

    run.reset_mock()
    with patch.dict(os.environ, {monitor.MONITOR_SECRET_ENV: "expected-secret"}, clear=True), patch.object(monitor, "run_war_monitor", run):
        wrong = type(
            "Request",
            (),
            {
                "headers": {monitor.MONITOR_SECRET_HEADER: "wrong-secret"},
                "_send_json": lambda self, status, payload: setattr(self, "sent", (status, payload)),
            },
        )()
        monitor.handler.do_POST(wrong)

    assert wrong.sent[0] == 401
    run.assert_not_called()
    assert "expected-secret" not in json.dumps(wrong.sent[1])


def test_all_three_repository_clans_are_processed_independently_and_snapshots_written():
    client = FakeClient({tag: race_payload(tag, 0) for tag in CLANS})
    storage = MemoryStorage()

    report = run_monitor(client, storage)

    assert report["ok"] is True
    assert report["processed_clans"] == 3
    assert report["snapshots_written"] == 3
    assert set(report["freshness"]) == set(CLANS)
    assert len(storage.snapshot_calls) == 3
    assert len(storage.previous_calls) == 3
    assert set(client.calls) == {
        (method, tag)
        for tag in CLANS
        for method in ("get_clan", "get_members", "get_current_river_race")
    }


def test_one_clan_error_is_partial_and_does_not_skip_the_other_clans():
    failed_tag = "GPCLVLPP"
    client = FakeClient(
        {tag: race_payload(tag, 0) for tag in CLANS},
        failures={("get_current_river_race", failed_tag)},
    )
    storage = MemoryStorage()

    report = run_monitor(client, storage)

    assert report["ok"] is False
    assert report["http_status"] == monitor.HTTP_STATUS_PARTIAL
    assert report["processed_clans"] == 3
    assert report["snapshots_written"] == 2
    assert report["freshness"][failed_tag]["current_river_race"]["status"] == "error"
    assert any(failed_tag in warning for warning in report["warnings"])
    assert {tag for _method, tag in client.calls} == set(CLANS)
    assert len(storage.snapshot_calls) == 2


def test_previous_snapshots_drive_t07_zero_to_one_two_and_three_events():
    values = {"9YP8UY": 1, "GPCLVLPP": 2, "RLQQQC99": 3}
    client = FakeClient({tag: race_payload(tag, 0) for tag in CLANS})
    storage = MemoryStorage()
    run_monitor(client, storage, timestamp=FIRST_CAPTURE, policy=True)

    client.races = {tag: race_payload(tag, count) for tag, count in values.items()}
    report = run_monitor(client, storage, timestamp=SECOND_CAPTURE, policy=True)

    event_types = {event["clan_tag"]: event["event_type"] for event in storage.events}
    assert event_types == {
        "9YP8UY": STATUS_SOLO_START_OBSERVED,
        "GPCLVLPP": STATUS_DUEL_FIRST_LIKELY,
        "RLQQQC99": STATUS_DUEL_FIRST_LIKELY,
    }
    assert report["events_created"] == 3
    assert report["notifications_pending"] == 3
    assert len(storage.notifications) == 3
    assert all(
        event["details"]["previous_decks_used_today"] == 0
        for event in storage.events
    )
    assert all(call["before_captured_at"] == SECOND_CAPTURE for call in storage.previous_calls[3:])


def test_duplicate_run_does_not_create_duplicate_events_or_pending_notifications():
    client = FakeClient({tag: race_payload(tag, 0) for tag in CLANS})
    storage = MemoryStorage()
    run_monitor(client, storage, timestamp=FIRST_CAPTURE, policy=True)
    client.races = {tag: race_payload(tag, 2) for tag in CLANS}

    first = run_monitor(client, storage, timestamp=SECOND_CAPTURE, policy=True)
    second = run_monitor(client, storage, timestamp=SECOND_CAPTURE, policy=True)

    assert first["events_created"] == 3
    assert first["notifications_pending"] == 3
    assert second["events_created"] == 0
    assert second["notifications_pending"] == 0
    assert len(storage.events) == 3
    assert len(storage.notifications) == 3
    assert len(storage.event_write_calls) == 3
    assert len(storage.notification_calls) == 3


def test_missing_metrics_are_preserved_as_none_and_do_not_make_a_duel_event():
    client = FakeClient({tag: race_payload(tag, 0, include_metrics=False) for tag in CLANS})
    storage = MemoryStorage()

    report = run_monitor(client, storage)

    row = storage.snapshots[0]
    assert row["decks_used_today"] is None
    assert row["fame"] is None
    assert report["events_created"] == 0
    assert storage.events == []


def test_colosseum_race_is_monitored_but_keeps_its_phase_type():
    client = FakeClient(
        {
            tag: race_payload(tag, 0)
            | {"periodType": "colosseum"}
            for tag in CLANS
        }
    )
    storage = MemoryStorage()

    report = run_monitor(client, storage)

    assert report["ok"] is True
    assert report["snapshots_written"] == 3
    assert all(clan["race"]["status"] == "active" for clan in report["clans"])
    assert all(clan["race"]["period_type"] == "colosseum" for clan in report["clans"])


@pytest.mark.parametrize(
    ("race", "expected_status"),
    [
        ("non_active", "inactive"),
        ({}, "empty"),
    ],
)
def test_non_active_or_empty_race_never_resets_or_creates_events(race, expected_status):
    client = FakeClient(
        {
            tag: (
                race_payload(tag, 2, state="war")
                if race == "non_active"
                else race
            )
            for tag in CLANS
        }
    )
    storage = MemoryStorage()

    report = run_monitor(client, storage)

    assert report["ok"] is True
    assert report["snapshots_written"] == 0
    assert report["events_created"] == 0
    assert storage.snapshot_calls == []
    assert storage.previous_calls == []
    assert storage.events == []
    assert all(clan["race"]["status"] == expected_status for clan in report["clans"])


def test_stale_race_is_explicit_and_fails_closed_without_a_false_event():
    stale_race = envelope(
        race_payload("9YP8UY", 2),
        stale=True,
        status="stale",
    )
    client = FakeClient({tag: stale_race for tag in CLANS})
    storage = MemoryStorage()

    report = run_monitor(client, storage)

    assert report["ok"] is False
    assert report["http_status"] == monitor.HTTP_STATUS_PARTIAL
    assert report["freshness"]["9YP8UY"]["current_river_race"]["status"] == "stale"
    assert report["snapshots_written"] == 0
    assert report["events_created"] == 0
    assert any("stale" in warning for warning in report["warnings"])


def test_notification_queue_is_disabled_by_default_and_no_webhook_is_called():
    client = FakeClient({tag: race_payload(tag, 0) for tag in CLANS})
    storage = MemoryStorage()
    run_monitor(client, storage)
    client.races = {tag: race_payload(tag, 2) for tag in CLANS}
    webhook_forbidden = Mock(side_effect=AssertionError("webhook must not be called"))

    report = monitor.run_war_monitor(
        client=client,
        storage=storage,
        clan_configs=CLANS,
        observed_at=SECOND_CAPTURE,
        write_notification_logs_fn=webhook_forbidden,
    )

    assert report["events_created"] == 3
    assert report["notifications_pending"] == 0
    webhook_forbidden.assert_not_called()


def test_response_contract_and_http_statuses_are_stable():
    client = FakeClient({tag: race_payload(tag, 0) for tag in CLANS})
    storage = MemoryStorage()
    report = run_monitor(client, storage)
    assert {
        "ok",
        "processed_clans",
        "snapshots_written",
        "events_created",
        "notifications_pending",
        "freshness",
        "warnings",
    }.issubset(report)
    assert report["http_status"] == 200

    success = type(
        "Request",
        (),
        {
            "headers": {monitor.MONITOR_SECRET_HEADER: "secret"},
            "_send_json": lambda self, status, payload: setattr(self, "sent", (status, payload)),
        },
    )()
    with patch.dict(os.environ, {monitor.MONITOR_SECRET_ENV: "secret"}, clear=True), patch.object(
        monitor,
        "run_war_monitor",
        return_value={"ok": True, "http_status": 200},
    ):
        monitor.handler.do_POST(success)
    assert success.sent[0] == 200

    partial = type(
        "Request",
        (),
        {
            "headers": {monitor.MONITOR_SECRET_HEADER: "secret"},
            "_send_json": lambda self, status, payload: setattr(self, "sent", (status, payload)),
        },
    )()
    with patch.dict(os.environ, {monitor.MONITOR_SECRET_ENV: "secret"}, clear=True), patch.object(
        monitor,
        "run_war_monitor",
        return_value={"ok": False, "http_status": monitor.HTTP_STATUS_PARTIAL},
    ):
        monitor.handler.do_POST(partial)
    assert partial.sent[0] == monitor.HTTP_STATUS_PARTIAL

    get_request = type(
        "Request",
        (),
        {
            "_send_json": lambda self, status, payload: setattr(self, "sent", (status, payload)),
        },
    )()
    monitor.handler.do_GET(get_request)
    assert get_request.sent[0] == 405


def test_secret_matches_uses_the_configured_value_without_returning_it():
    with patch.dict(os.environ, {monitor.MONITOR_SECRET_ENV: "a-secret"}, clear=True):
        assert monitor.secret_matches("a-secret") is True
        assert monitor.secret_matches("different") is False
    assert monitor.secret_matches(123) is False
    assert "a-secret" not in repr(monitor.secret_matches)


def test_secret_in_an_upstream_exception_is_not_returned_or_logged(caplog):
    secret = "upstream-exception-secret"
    client = FakeClient(
        {tag: race_payload(tag, 0) for tag in CLANS},
        failures={("get_current_river_race", "9YP8UY")},
        error_message=secret,
    )
    storage = MemoryStorage()

    report = monitor.run_war_monitor(
        client=client,
        storage=storage,
        clan_configs=CLANS,
        observed_at=FIRST_CAPTURE,
    )

    encoded = json.dumps(report, sort_keys=True)
    assert secret not in encoded
    assert secret not in caplog.text
