import json
import os
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest

from api.clash_client import ClashResponse, ResponseMetadata
from api.duel_first import (
    STATUS_API_STALE,
    STATUS_DUEL_FIRST_LIKELY,
)
import api.war_status as war_status


CLAN_TAG = "9YP8UY"
RACE_CREATED_AT = "2026-08-27T08:00:00Z"
OBSERVED_AT = "2026-08-27T08:11:00Z"


def envelope(payload, *, stale=False, empty=False):
    return ClashResponse(
        data=payload,
        metadata=ResponseMetadata(
            source="test_clash",
            fetched_at=OBSERVED_AT,
            is_stale=stale,
            stale_reason="upstream_server_error" if stale else None,
            error_code="upstream_server_error" if stale else None,
            endpoint="/safe/test-endpoint",
            empty=empty,
        ),
    )


def participant(tag="#PLAYER1", name="Alice", decks_today=2, *, include_metrics=True):
    row = {
        "tag": tag,
        "name": name,
        "role": "member",
        "decksUsed": 8,
        "decksUsedToday": decks_today,
        "fame": 2500,
        "repairPoints": 100,
        "boatAttacks": 1,
        "boatAttacksToday": 1,
        "boatDefenses": 2,
        "boatDefensesToday": 1,
    }
    if not include_metrics:
        row.pop("decksUsedToday")
        row.pop("fame")
    return row


def race_payload(*, decks_today=2, include_metrics=True):
    own = {
        "tag": f"#{CLAN_TAG}",
        "name": "Brabant Royale",
        "members": 2,
        "fame": 2500,
        "repairPoints": 100,
        "decksUsedToday": decks_today,
        "participants": [
            participant(decks_today=decks_today, include_metrics=include_metrics)
        ],
    }
    opponent = {
        "tag": "#OPPONENT",
        "name": "Opponent Clan",
        "fame": 2200,
        "repairPoints": 90,
        "decksUsedToday": 3,
        "participants": [],
    }
    return {
        "state": "warDay",
        "seasonId": 134,
        "sectionIndex": 3,
        "periodIndex": 2,
        "periodType": "war",
        "createdDate": "20260827T080000.000Z",
        "clan": own,
        "clans": [own, opponent],
    }


class FakeClient:
    def __init__(self, *, race=None, failures=()):
        self.race = race if race is not None else envelope(race_payload())
        self.failures = set(failures)
        self.calls = []

    def _get(self, operation):
        self.calls.append(operation)
        if operation in self.failures:
            raise RuntimeError("transport failed with api-key-secret")
        if operation == "clan":
            return envelope(
                {"tag": f"#{CLAN_TAG}", "name": "Brabant Royale", "members": 2}
            )
        if operation == "members":
            return envelope(
                {
                    "items": [
                        {"tag": "#PLAYER1", "name": "Alice", "role": "leader"},
                        {"tag": "#PLAYER2", "name": "Bob", "role": "member"},
                    ]
                }
            )
        return self.race

    def get_clan(self, tag):
        assert tag == CLAN_TAG
        return self._get("clan")

    def get_members(self, tag):
        assert tag == CLAN_TAG
        return self._get("members")

    def get_current_river_race(self, tag):
        assert tag == CLAN_TAG
        return self._get("race")


class ReadOnlyHistory:
    def __init__(self, previous=None):
        self.previous = previous
        self.calls = []

    def read_previous_player_snapshot(
        self,
        clan_tag,
        race_created_at,
        period_index,
        player_tag,
        *,
        before_captured_at=None,
    ):
        self.calls.append(
            (clan_tag, race_created_at, period_index, player_tag, before_captured_at)
        )
        if self.previous is None:
            return {"ok": True, "status": "empty", "snapshot": None}
        return {"ok": True, "status": "fresh", "snapshot": dict(self.previous)}


def build(client, *, history=None, **kwargs):
    return war_status.build_war_status_payload(
        CLAN_TAG,
        client=client,
        storage=history,
        observed_at=OBSERVED_AT,
        **kwargs,
    )


def test_live_contract_uses_t02_and_t07_and_never_writes():
    history = ReadOnlyHistory(
        {
            "clan_tag": CLAN_TAG,
            "race_created_at": RACE_CREATED_AT,
            "period_index": 2,
            "player_tag": "PLAYER1",
            "decks_used_today": 0,
        }
    )
    client = FakeClient()

    payload = build(client, history=history)

    assert payload["status"] == "live"
    assert payload["data_status"] in {"fresh", "partial"}
    assert set(
        (
            "race_context",
            "clan_rows",
            "player_rows",
            "duel_first_summary",
            "alerts",
            "freshness",
            "data_quality",
        )
    ).issubset(payload)
    player = payload["player_rows"][0]
    assert {
        "player_tag",
        "name",
        "role",
        "decks_used_today",
        "decks_remaining_today",
        "duel_first_status",
        "status_confidence",
        "observed_at",
    }.issubset(player)
    assert player["player_tag"] == "PLAYER1"
    assert player["decks_used_today"] == 2
    assert player["decks_remaining_today"] == 2
    assert player["duel_first_status"] == "redacted"
    assert player["status_confidence"] == "redacted"
    assert history.calls and history.calls[0][0] == CLAN_TAG
    assert not any("write" in name.lower() for name in dir(history))
    assert payload["data_quality"]["read_only"] is True


def test_stale_current_data_is_explicit_and_does_not_create_duel_alerts():
    client = FakeClient(race=envelope(race_payload(decks_today=2), stale=True))
    history = ReadOnlyHistory(
        {
            "race_created_at": RACE_CREATED_AT,
            "period_index": 2,
            "player_tag": "PLAYER1",
            "decks_used_today": 0,
        }
    )

    payload = build(client, history=history)

    assert payload["status"] == "stale"
    assert payload["freshness"]["race"]["status"] == "stale"
    assert payload["player_rows"][0]["duel_first_status"] == STATUS_API_STALE
    assert payload["alerts"]
    assert all("player_tag" not in alert for alert in payload["alerts"])
    assert history.calls == []


def test_error_state_is_safe_and_does_not_include_exception_or_credentials():
    secret = "api-key-secret"
    client = FakeClient(failures={"clan", "members", "race"})

    with patch.dict(os.environ, {"CLASH_ROYALE_API_KEY": secret}, clear=False):
        payload = build(client)

    encoded = json.dumps(payload, sort_keys=True)
    assert payload["status"] == "error"
    assert payload["ok"] is False
    assert secret not in encoded
    assert "transport failed" not in encoded
    assert all("message" not in error for error in payload["data_quality"]["errors"])


def test_empty_state_keeps_metrics_unknown_instead_of_zero():
    client = FakeClient(race=envelope({}, empty=True))

    payload = build(client, history=ReadOnlyHistory())

    assert payload["status"] == "empty"
    assert payload["race_context"]["data_status"] == "empty"
    assert payload["clan_rows"] == []
    assert payload["player_rows"] == []
    assert payload["duel_first_summary"]["players_classified"] == 0
    assert all("player_tag" not in alert for alert in payload["alerts"])


def test_missing_metrics_remain_none_and_are_reported():
    client = FakeClient(
        race=envelope(race_payload(decks_today=0, include_metrics=False))
    )

    payload = build(client, history=ReadOnlyHistory())

    player = payload["player_rows"][0]
    assert player["decks_used_today"] is None
    assert player["decks_remaining_today"] is None
    assert player["duel_first_status"] != STATUS_DUEL_FIRST_LIKELY
    assert "decks_used_today" in payload["data_quality"]["missing_fields"]
    assert "decks_remaining_today" in payload["data_quality"]["missing_fields"]
    assert any(alert["type"] == "data_quality" for alert in payload["alerts"])


def test_public_alerts_are_aggregated_without_player_identity():
    history = ReadOnlyHistory(
        {
            "race_created_at": RACE_CREATED_AT,
            "period_index": 2,
            "player_tag": "PLAYER1",
            "decks_used_today": 0,
        }
    )
    payload = build(FakeClient(), history=history)

    assert payload["alerts_scope"] == "public_aggregate"
    assert payload["leader_view"] is False
    assert {
        "type": "duel_first",
        "scope": "public_aggregate",
        "status": STATUS_DUEL_FIRST_LIKELY,
        "count": 1,
    } in payload["alerts"]
    assert all("player_tag" not in alert for alert in payload["alerts"])
    assert "PLAYER1" not in json.dumps(payload["alerts"])
    assert "Alice" not in json.dumps(payload["alerts"])


def test_individual_alerts_require_explicit_server_verified_leader_view():
    history = ReadOnlyHistory(
        {
            "race_created_at": RACE_CREATED_AT,
            "period_index": 2,
            "player_tag": "PLAYER1",
            "decks_used_today": 0,
        }
    )
    with patch.dict(
        os.environ,
        {war_status.WAR_STATUS_LEADER_SECRET_ENV: "leader-secret"},
        clear=True,
    ):
        public = build(FakeClient(), history=history)
        assert war_status.leader_request_authorized(
            "?clan=%239YP8UY&view=leader",
            {war_status.WAR_STATUS_LEADER_HEADER: "wrong"},
        ) is False
        assert war_status.leader_request_authorized(
            "?clan=%239YP8UY&view=leader",
            {war_status.WAR_STATUS_LEADER_HEADER: "leader-secret"},
        ) is True
        leader = build(FakeClient(), history=history, leader_verified=True)

    assert public["alerts"] and "player_tag" not in public["alerts"][0]
    assert leader["alerts_scope"] == "leader"
    assert leader["leader_view"] is True
    assert leader["alerts"][0]["player_tag"] == "PLAYER1"
    assert leader["player_rows"][0]["duel_first_status"] == STATUS_DUEL_FIRST_LIKELY
    assert leader["player_rows"][0]["status_confidence"] == "medium"
    assert "leader-secret" not in json.dumps(leader)


def test_handler_denies_leader_request_before_upstream_and_accepts_public_get():
    run = Mock(side_effect=AssertionError("upstream must not be called"))
    request = type(
        "Request",
        (),
        {
            "path": "?clan=%239YP8UY&view=leader",
            "headers": {war_status.WAR_STATUS_LEADER_HEADER: "wrong"},
            "_send_json": lambda self, status, payload: setattr(
                self, "sent", (status, payload)
            ),
        },
    )()
    with patch.dict(
        os.environ,
        {war_status.WAR_STATUS_LEADER_SECRET_ENV: "leader-secret"},
        clear=True,
    ), patch.object(war_status, "build_war_status_payload", run):
        war_status.handler.do_GET(request)

    assert request.sent[0] == war_status.HTTP_STATUS_FORBIDDEN
    run.assert_not_called()
    assert "leader-secret" not in json.dumps(request.sent[1])


def test_invalid_clan_is_rejected_before_client_use_and_tag_is_canonicalized():
    client = Mock()
    with pytest.raises(war_status.InvalidClanTagError):
        war_status.build_war_status_payload(
            "not-a-clan/tag",
            client=client,
        )
    client.assert_not_called()
    assert war_status.validate_clan_tag("%239yp8uy") == CLAN_TAG


def test_handler_invalid_clan_and_non_get_are_safe_contracts():
    invalid = type(
        "Request",
        (),
        {
            "path": "?clan=not-a-clan",
            "headers": {},
            "_send_json": lambda self, status, payload: setattr(
                self, "sent", (status, payload)
            ),
        },
    )()
    war_status.handler.do_GET(invalid)
    assert invalid.sent[0] == war_status.HTTP_STATUS_BAD_REQUEST
    assert invalid.sent[1]["status"] == "error"

    method_not_allowed = type(
        "Request",
        (),
        {
            "_send_json": lambda self, status, payload: setattr(
                self, "sent", (status, payload)
            ),
        },
    )()
    war_status.handler.do_POST(method_not_allowed)
    assert method_not_allowed.sent[0] == war_status.HTTP_STATUS_METHOD_NOT_ALLOWED


def test_secrets_are_not_in_freshness_or_public_output():
    secrets = {
        "CLASH_ROYALE_API_KEY": "clash-api-secret",
        "SUPABASE_SECRET_KEY": "database-secret",
        war_status.WAR_STATUS_LEADER_SECRET_ENV: "leader-secret",
    }
    with patch.dict(os.environ, secrets, clear=False):
        payload = build(FakeClient(), history=ReadOnlyHistory())

    encoded = json.dumps(payload, sort_keys=True)
    assert all(secret not in encoded for secret in secrets.values())
    assert "Authorization" not in encoded
    assert "Bearer" not in encoded


def test_clock_can_supply_a_deterministic_route_observation_time():
    client = FakeClient()
    payload = war_status.build_war_status_payload(
        CLAN_TAG,
        client=client,
        storage=ReadOnlyHistory(),
        clock=lambda: datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc),
    )

    assert payload["player_rows"][0]["observed_at"] == OBSERVED_AT
    assert payload["race_context"]["route_observed_at"].startswith("2026-08-27T09:00:00")
