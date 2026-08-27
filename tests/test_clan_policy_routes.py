import io
import json
from unittest.mock import patch

import api.clan_policy as clan_policy_route
import api.leader_decisions as decision_route
from clan_policy import (
    ADMIN_KEY_HEADER,
    DEFAULT_POLICY,
    ClanPolicyStorageError,
    POLICY_FIELDS,
)


CLAN_TAG = "9YP8UY"
ADMIN_KEY = "leader-admin-key"


def fake_request(*, path, headers=None, body=None):
    request = type("Request", (), {})()
    request.path = path
    request.headers = headers or {}
    raw_body = json.dumps(body).encode("utf-8") if body is not None else b""
    request.rfile = io.BytesIO(raw_body)
    request.headers.setdefault("Content-Length", str(len(raw_body)))
    request._send_json = lambda status, payload: setattr(
        request,
        "sent",
        (status, payload),
    )
    return request


def policy_result(*, ok=True, source="stored"):
    policy = {"clan_tag": CLAN_TAG, **DEFAULT_POLICY}
    return {
        "ok": ok,
        "status": "stored" if ok else "error",
        "data_status": "stored" if ok else "error",
        "policy_source": source,
        "clan_tag": CLAN_TAG,
        "policy": policy,
    }


def test_public_policy_route_contains_only_non_sensitive_configuration():
    request = fake_request(path=f"/api/clan_policy?clan=%23{CLAN_TAG}")
    with patch.object(
        clan_policy_route, "read_clan_policy", return_value=policy_result()
    ):
        clan_policy_route.handler.do_GET(request)

    status, payload = request.sent
    encoded = json.dumps(payload)
    assert status == 200
    assert payload["policy"]["clan_tag"] == CLAN_TAG
    assert set(POLICY_FIELDS).issubset(payload["policy"])
    assert "actor" not in encoded
    assert "reason" not in encoded
    assert "decisions" not in encoded


def test_policy_write_requires_admin_key_before_reading_body_or_storage():
    request = fake_request(
        path="/api/clan_policy",
        body={"clan_tag": CLAN_TAG, "duel_first_enabled": True},
    )
    with patch.object(clan_policy_route, "write_clan_policy") as mocked_write:
        clan_policy_route.handler.do_POST(request)

    assert request.sent == (403, {"ok": False, "error": "Unauthorized."})
    mocked_write.assert_not_called()


def test_policy_write_forwards_existing_admin_key_only_to_storage():
    request = fake_request(
        path="/api/clan_policy",
        headers={ADMIN_KEY_HEADER: ADMIN_KEY},
        body={"clan_tag": CLAN_TAG, "duel_first_enabled": True},
    )
    with patch.object(
        clan_policy_route,
        "write_clan_policy",
        return_value=policy_result(),
    ) as mocked_write:
        clan_policy_route.handler.do_POST(request)

    assert request.sent[0] == 200
    assert mocked_write.call_args.args[1] == ADMIN_KEY
    assert ADMIN_KEY not in json.dumps(request.sent[1])


def test_policy_route_maps_storage_failure_to_safe_response():
    request = fake_request(
        path="/api/clan_policy",
        headers={ADMIN_KEY_HEADER: ADMIN_KEY},
        body={"clan_tag": CLAN_TAG},
    )
    with patch.object(
        clan_policy_route,
        "write_clan_policy",
        side_effect=ClanPolicyStorageError("forbidden"),
    ):
        clan_policy_route.handler.do_POST(request)

    assert request.sent == (403, {"ok": False, "error": "Unauthorized."})


def test_public_leader_decision_get_is_rejected_without_sensitive_fields():
    request = fake_request(path=f"/api/leader_decisions?clan=%23{CLAN_TAG}")
    with patch.object(decision_route, "read_leader_decisions") as mocked_read:
        decision_route.handler.do_GET(request)

    status, payload = request.sent
    assert status == 403
    assert payload == {"ok": False, "error": "Unauthorized."}
    assert "actor" not in json.dumps(payload)
    assert "reason" not in json.dumps(payload)
    assert "decisions" not in payload
    mocked_read.assert_not_called()


def test_leader_decision_get_rejects_wrong_admin_key_without_leaking_audit_data():
    request = fake_request(
        path=f"/api/leader_decisions?clan=%23{CLAN_TAG}",
        headers={ADMIN_KEY_HEADER: "wrong-key"},
    )
    with patch.object(
        decision_route,
        "read_leader_decisions",
        side_effect=ClanPolicyStorageError("forbidden"),
    ):
        decision_route.handler.do_GET(request)

    status, payload = request.sent
    assert status == 403
    assert payload == {"ok": False, "error": "Unauthorized."}
    assert "actor" not in json.dumps(payload)
    assert "reason" not in json.dumps(payload)


def test_leader_decision_post_requires_admin_key_and_does_not_echo_body():
    request = fake_request(
        path="/api/leader_decisions",
        body={
            "clan_tag": CLAN_TAG,
            "actor": "leader-1",
            "decision_type": "promotion",
            "reason": "Audit reason",
        },
    )
    with patch.object(decision_route, "write_leader_decision") as mocked_write:
        decision_route.handler.do_POST(request)

    assert request.sent == (403, {"ok": False, "error": "Unauthorized."})
    assert "leader-1" not in json.dumps(request.sent[1])
    assert "Audit reason" not in json.dumps(request.sent[1])
    mocked_write.assert_not_called()


def test_leader_decision_post_returns_audit_fields_only_after_admin_auth():
    request = fake_request(
        path="/api/leader_decisions",
        headers={ADMIN_KEY_HEADER: ADMIN_KEY},
        body={
            "clan_tag": CLAN_TAG,
            "player_tag": "#PLAYER1",
            "actor": "leader-1",
            "decision_type": "manual_correction",
            "reason": "Corrected after review",
            "related_race_key": "race-1",
            "created_at": "2026-08-27T09:00:00Z",
            "idempotency_key": "manual-correction-1",
        },
    )
    result = {
        "ok": True,
        "status": "stored",
        "action": "accepted",
        "decision": {
            "clan_tag": CLAN_TAG,
            "player_tag": "PLAYER1",
            "actor": "leader-1",
            "decision_type": "manual_correction",
            "reason": "Corrected after review",
            "related_race_key": "race-1",
            "created_at": "2026-08-27T09:00:00Z",
            "idempotency_key": "manual-correction-1",
        },
    }
    with patch.object(
        decision_route,
        "write_leader_decision",
        return_value=result,
    ) as mocked_write:
        decision_route.handler.do_POST(request)

    assert request.sent[0] == 200
    assert request.sent[1]["decision"]["actor"] == "leader-1"
    assert request.sent[1]["decision"]["reason"] == "Corrected after review"
    assert mocked_write.call_args.args[1] == ADMIN_KEY


def test_leader_decision_get_returns_only_requested_clan_from_storage():
    request = fake_request(
        path=f"/api/leader_decisions?clan=%23{CLAN_TAG}&limit=10",
        headers={ADMIN_KEY_HEADER: ADMIN_KEY},
    )
    result = {
        "ok": True,
        "status": "stored",
        "data_status": "stored",
        "clan_tag": CLAN_TAG,
        "decisions": [
            {
                "clan_tag": CLAN_TAG,
                "actor": "leader-1",
                "decision_type": "exemption",
                "reason": "Holiday",
                "created_at": "2026-08-27T09:00:00Z",
                "idempotency_key": "exemption-1",
            }
        ],
        "count": 1,
        "limit": 10,
    }
    with patch.object(
        decision_route,
        "read_leader_decisions",
        return_value=result,
    ) as mocked_read:
        decision_route.handler.do_GET(request)

    assert request.sent[0] == 200
    assert request.sent[1]["decisions"][0]["clan_tag"] == CLAN_TAG
    assert mocked_read.call_args.args == (CLAN_TAG, ADMIN_KEY)
    assert mocked_read.call_args.kwargs["limit"] == 10
