import json
from unittest.mock import Mock, patch

import pytest

import clan_policy as policy


SUPABASE_URL = "https://example.supabase.co"
PUBLIC_KEY = "sb_publishable_test-key"
ADMIN_KEY = "leader-admin-key"
CLAN_A = "9YP8UY"
CLAN_B = "GPCLVLPP"


def response(status_code, payload=None, *, content=True):
    mocked = Mock(status_code=status_code)
    mocked.content = b"{}" if content else b""
    if payload is not None:
        mocked.json.return_value = payload
    mocked.headers = {}
    return mocked


def test_policy_defaults_are_explicit_and_complete():
    result = policy.validate_policy_payload({"clan_tag": f"#{CLAN_A.lower()}"})

    assert result["clan_tag"] == CLAN_A
    assert {
        field: result[field] for field in policy.POLICY_FIELDS
    } == policy.DEFAULT_POLICY
    assert all(result[field] is not None for field in policy.POLICY_FIELDS)


def test_policy_values_are_normalized_within_allowed_ranges():
    result = policy.validate_policy_payload(
        {
            "clan_tag": CLAN_A,
            "duel_first_enabled": True,
            "duel_first_alert_after_utc": "7:05",
            "promotion_window_weeks": "4",
            "promotion_min_average": 2750.0,
            "promotion_min_reliability": "97.25",
            "promotion_min_observed_races": 3,
            "demotion_window_weeks": 12,
            "demotion_max_missed_attacks": 8,
            "trial_races_required": 4,
        }
    )

    assert result == {
        "clan_tag": CLAN_A,
        "duel_first_enabled": True,
        "duel_first_alert_after_utc": "07:05:00Z",
        "promotion_window_weeks": 4,
        "promotion_min_average": 2750,
        "promotion_min_reliability": 97.25,
        "promotion_min_observed_races": 3,
        "demotion_window_weeks": 12,
        "demotion_max_missed_attacks": 8,
        "trial_races_required": 4,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("duel_first_enabled", "yes"),
        ("duel_first_alert_after_utc", "24:00:00Z"),
        ("promotion_window_weeks", 3),
        ("promotion_min_average", -1),
        ("promotion_min_reliability", 100.01),
        ("promotion_min_observed_races", 0),
        ("demotion_window_weeks", 0),
        ("demotion_max_missed_attacks", -1),
        ("trial_races_required", 0),
    ],
)
def test_invalid_policy_values_are_rejected(field, value):
    with pytest.raises(ValueError):
        policy.validate_policy_payload({"clan_tag": CLAN_A, field: value})


def test_read_missing_policy_uses_defaults_and_clan_filter():
    with patch("clan_policy.requests.get") as mocked_get:
        mocked_get.return_value = response(200, [])
        result = policy.read_clan_policy(
            f"#{CLAN_A.lower()}",
            supabase_url=SUPABASE_URL,
            api_key=PUBLIC_KEY,
        )

    assert result["ok"] is True
    assert result["status"] == "defaults"
    assert result["policy_source"] == "defaults"
    assert result["policy"]["clan_tag"] == CLAN_A
    assert all(
        result["policy"].get(field) is not None for field in policy.POLICY_FIELDS
    )
    assert mocked_get.call_args.kwargs["params"]["clan_tag"] == f"eq.{CLAN_A}"


def test_read_policy_does_not_cross_clan_isolation_when_upstream_returns_wrong_row():
    row = {
        "clan_tag": CLAN_B,
        "duel_first_enabled": True,
        "promotion_window_weeks": 2,
    }
    with patch("clan_policy.requests.get") as mocked_get:
        mocked_get.return_value = response(200, [row])
        result = policy.read_clan_policy(
            CLAN_A,
            supabase_url=SUPABASE_URL,
            api_key=PUBLIC_KEY,
        )

    assert result["status"] == "defaults"
    assert result["policy"]["clan_tag"] == CLAN_A
    assert (
        result["policy"]["duel_first_enabled"]
        is policy.DEFAULT_POLICY["duel_first_enabled"]
    )


def test_policy_write_is_idempotent_upsert_and_keeps_admin_key_out_of_url_and_body():
    with patch("clan_policy.requests.post") as mocked_post:
        mocked_post.return_value = response(201, content=False)
        result = policy.write_clan_policy(
            {
                "clan_tag": CLAN_A,
                "duel_first_enabled": True,
                "promotion_window_weeks": 4,
            },
            ADMIN_KEY,
            supabase_url=SUPABASE_URL,
            api_key=PUBLIC_KEY,
        )

    request_url = mocked_post.call_args.args[0]
    sent = mocked_post.call_args.kwargs["json"]
    headers = mocked_post.call_args.kwargs["headers"]
    assert "on_conflict=clan_tag" in request_url
    assert ADMIN_KEY not in request_url
    assert ADMIN_KEY not in json.dumps(sent)
    assert headers[policy.ADMIN_KEY_HEADER] == ADMIN_KEY
    assert sent["duel_first_enabled"] is True
    assert sent["promotion_window_weeks"] == 4
    assert set(policy.POLICY_FIELDS).issubset(sent)
    assert ADMIN_KEY not in json.dumps(result)


def test_policy_write_rejects_supabase_admin_key_failure_without_leaking_key():
    with patch("clan_policy.requests.post") as mocked_post:
        mocked_post.return_value = response(403, content=False)
        with pytest.raises(policy.ClanPolicyStorageError) as raised:
            policy.write_clan_policy(
                {"clan_tag": CLAN_A},
                ADMIN_KEY,
                supabase_url=SUPABASE_URL,
                api_key=PUBLIC_KEY,
                max_retries=0,
            )

    assert raised.value.code == "forbidden"
    assert ADMIN_KEY not in str(raised.value)
    assert SUPABASE_URL not in str(raised.value)


def test_decision_types_and_audit_fields_are_validated_and_preserved():
    for index, decision_type in enumerate(sorted(policy.DECISION_TYPES)):
        result = policy.validate_decision_payload(
            {
                "clan_tag": CLAN_A,
                "player_tag": "#PLAYER1" if index % 2 == 0 else None,
                "actor": "leader-1",
                "decision_type": decision_type,
                "reason": "Besluit vastgelegd",
                "related_race_key": "134-3-2026-08-27" if index % 2 == 0 else None,
                "created_at": "2026-08-27T09:00:00+00:00",
                "idempotency_key": f"decision-{index}",
            }
        )

        assert result["decision_type"] == decision_type
        assert result["actor"] == "leader-1"
        assert result["reason"] == "Besluit vastgelegd"
        assert result["created_at"] == "2026-08-27T09:00:00Z"
        assert result["clan_tag"] == CLAN_A


@pytest.mark.parametrize(
    "payload",
    [
        {"actor": "leader", "decision_type": "unknown", "reason": "x"},
        {"actor": "", "decision_type": "promotion", "reason": "x"},
        {"actor": "leader", "decision_type": "promotion", "reason": ""},
        {
            "actor": "x" * (policy.MAX_ACTOR_LENGTH + 1),
            "decision_type": "promotion",
            "reason": "x",
        },
        {
            "actor": "leader",
            "decision_type": "promotion",
            "reason": "x" * (policy.MAX_REASON_LENGTH + 1),
        },
        {
            "actor": "leader",
            "decision_type": "promotion",
            "reason": "x",
            "created_at": "not-a-timestamp",
        },
    ],
)
def test_invalid_decision_values_are_rejected(payload):
    with pytest.raises(ValueError):
        policy.validate_decision_payload({"clan_tag": CLAN_A, **payload})


def test_decision_write_uses_append_only_ignore_duplicate_key():
    payload = {
        "clan_tag": CLAN_A,
        "player_tag": "#PLAYER1",
        "actor": "leader-1",
        "decision_type": "promotion",
        "reason": "Voldoende observaties",
        "related_race_key": "134-3-2026-08-27",
        "created_at": "2026-08-27T09:00:00Z",
        "idempotency_key": "promotion-player1-20260827",
    }
    with patch("clan_policy.requests.post") as mocked_post:
        mocked_post.return_value = response(201, content=False)
        result = policy.write_leader_decision(
            payload,
            ADMIN_KEY,
            supabase_url=SUPABASE_URL,
            api_key=PUBLIC_KEY,
        )

    sent = mocked_post.call_args.kwargs["json"]
    headers = mocked_post.call_args.kwargs["headers"]
    assert "on_conflict=clan_tag,idempotency_key" in mocked_post.call_args.args[0]
    assert headers[policy.ADMIN_KEY_HEADER] == ADMIN_KEY
    assert "resolution=ignore-duplicates" in headers["Prefer"]
    assert sent["player_tag"] == "PLAYER1"
    assert sent["decision_type"] == "promotion"
    assert sent["created_at"] == "2026-08-27T09:00:00Z"
    assert result["decision"]["idempotency_key"] == "promotion-player1-20260827"
    assert ADMIN_KEY not in json.dumps(result)


def test_read_decisions_filters_rows_again_after_supabase_query():
    rows = [
        {
            "id": 2,
            "clan_tag": CLAN_B,
            "player_tag": "PLAYER2",
            "actor": "other-leader",
            "decision_type": "reject",
            "reason": "Andere clan",
            "related_race_key": None,
            "created_at": "2026-08-27T09:02:00Z",
            "idempotency_key": "other-1",
        },
        {
            "id": 1,
            "clan_tag": CLAN_A,
            "player_tag": "PLAYER1",
            "actor": "leader-1",
            "decision_type": "exemption",
            "reason": "Vakantie",
            "related_race_key": "race-1",
            "created_at": "2026-08-27T09:00:00Z",
            "idempotency_key": "a-1",
        },
    ]
    with patch("clan_policy.requests.get") as mocked_get:
        mocked_get.return_value = response(200, rows)
        result = policy.read_leader_decisions(
            CLAN_A,
            ADMIN_KEY,
            supabase_url=SUPABASE_URL,
            api_key=PUBLIC_KEY,
        )

    assert result["count"] == 1
    assert result["decisions"][0]["clan_tag"] == CLAN_A
    assert result["decisions"][0]["actor"] == "leader-1"
    assert result["decisions"][0]["reason"] == "Vakantie"
    assert mocked_get.call_args.kwargs["params"]["clan_tag"] == f"eq.{CLAN_A}"


def test_public_policy_projection_repairs_missing_fields_without_nulls():
    result = policy.public_policy_payload(
        {
            "ok": True,
            "status": "stored",
            "clan_tag": CLAN_A,
            "policy": {"clan_tag": CLAN_A},
        }
    )

    assert result["policy"]["clan_tag"] == CLAN_A
    assert all(
        result["policy"].get(field) is not None for field in policy.POLICY_FIELDS
    )
