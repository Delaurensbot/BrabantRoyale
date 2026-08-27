import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import api.join_data as join_data
import supabase_history


SUPABASE_URL = "https://example.supabase.co"
SERVER_KEY = "sb_secret_roster-test-key"
PUBLIC_KEY = "sb_publishable_roster-test-key"
CLAN_A = "9YP8UY"
CLAN_B = "GPCLVLPP"


def storage_response(status_code, payload=None, *, content=True, headers=None):
    response = Mock(
        status_code=status_code,
        content=b"{}" if content else b"",
        headers=headers or {},
    )
    if payload is not None:
        response.json.return_value = payload
    return response


def roster_row(
    clan=CLAN_A,
    player="PLAYER1",
    captured="2026-08-01T12:00:00Z",
    *,
    name="Alice",
    role="member",
    trophies=6000,
):
    return {
        "clan_tag": clan,
        "player_tag": player,
        "player_name": name,
        "role": role,
        "trophies": trophies,
        "seen_at": captured,
        "captured_at": captured,
    }


def member_payload(*members):
    return {
        "items": [
            {
                "tag": tag,
                "name": name,
                "role": role,
                "trophies": trophies,
            }
            for tag, name, role, trophies in members
        ]
    }


def test_tag_normalization_and_name_changes_keep_one_identity():
    rows = join_data.build_roster_snapshot_rows(
        [
            {"tag": "%23player-1", "name": "Old name", "role": "member"},
            {"tag": "#PLAYER1", "name": "New name", "role": "elder"},
        ],
        f"#{CLAN_A.lower()}",
        captured_at="2026-08-01T12:00:00Z",
    )

    assert join_data.normalize_player_tag("%23p-layer1") == "PLAYER1"
    assert len(rows) == 1
    assert rows[0]["player_tag"] == "PLAYER1"
    assert rows[0]["player_name"] == "New name"

    history = join_data.calculate_roster_history(
        [
            roster_row(captured="2026-08-01T12:00:00Z", name="Old name"),
            roster_row(captured="2026-08-02T12:00:00Z", name="New name"),
        ],
        CLAN_A,
    )
    assert len(history["players"]) == 1
    assert history["players"][0]["player_tag"] == "PLAYER1"
    assert history["players"][0]["player_name"] == "New name"
    assert history["joins"] == []


def test_history_isolated_by_clan():
    rows = [
        roster_row(clan=CLAN_A, player="PLAYER1"),
        roster_row(clan=CLAN_B, player="PLAYER1", name="Other clan"),
    ]

    history_a = join_data.calculate_roster_history(rows, f"#{CLAN_A.lower()}")
    history_b = join_data.calculate_roster_history(rows, CLAN_B)

    assert history_a["clan_tag"] == CLAN_A
    assert [row["player_name"] for row in history_a["players"]] == ["Alice"]
    assert [row["player_name"] for row in history_b["players"]] == ["Other clan"]


def test_roster_storage_upserts_and_deduplicates_natural_keys():
    with patch("supabase_history.requests.post") as mocked_post:
        mocked_post.return_value = storage_response(201, content=False)
        result = supabase_history.write_roster_snapshots(
            [
                roster_row(name="Old", role="member"),
                roster_row(name="New", role="elder"),
                roster_row(clan=CLAN_B, name="Same tag in another clan"),
            ],
            supabase_url=SUPABASE_URL,
            api_key=SERVER_KEY,
        )

    assert result["status"] == "ok"
    sent = mocked_post.call_args.kwargs["json"]
    assert len(sent) == 2
    assert sent[0]["player_tag"] == "PLAYER1"
    assert sent[0]["player_name"] == "New"
    assert set(
        ["clan_tag", "player_tag", "player_name", "role", "trophies", "seen_at", "captured_at"]
    ).issubset(sent[0])
    assert "on_conflict=clan_tag,player_tag,captured_at" in mocked_post.call_args.args[0]


def test_roster_storage_read_filters_clans_and_keeps_missing_trophies_unknown():
    with patch("supabase_history.requests.get") as mocked_get:
        mocked_get.return_value = storage_response(
            200,
            [
                roster_row(trophies=None),
                roster_row(clan=CLAN_B, name="Must not cross boundary"),
            ],
        )
        result = supabase_history.read_roster_snapshots(
            f"#{CLAN_A.lower()}",
            supabase_url=SUPABASE_URL,
            api_key=SERVER_KEY,
        )

    assert result["status"] == "fresh"
    assert result["snapshots"] == [roster_row(trophies=None)]
    assert result["snapshots"][0]["trophies"] is None
    assert mocked_get.call_args.kwargs["params"]["clan_tag"] == f"eq.{CLAN_A}"


def test_roster_storage_retries_with_bounded_attempts_and_safe_error():
    with patch("supabase_history.requests.post") as mocked_post:
        mocked_post.side_effect = [
            storage_response(503, content=False),
            storage_response(201, content=False),
        ]
        sleep = Mock()
        result = supabase_history.write_roster_snapshot(
            roster_row(),
            supabase_url=SUPABASE_URL,
            api_key=SERVER_KEY,
            max_retries=1,
            backoff_factor=0.1,
            max_backoff=0.2,
            sleep=sleep,
        )

    assert result["status"] == "ok"
    assert result["attempts"] == 2
    assert mocked_post.call_count == 2
    assert [call.args[0] for call in sleep.call_args_list] == [0.1]

    with patch("supabase_history.requests.post") as mocked_post:
        mocked_post.return_value = storage_response(503, content=False)
        error = supabase_history.write_roster_snapshot(
            roster_row(),
            supabase_url=SUPABASE_URL,
            api_key=SERVER_KEY,
            max_retries=0,
        )
    assert error["status"] == "error"
    assert error["error"] == "upstream_server_error"
    assert SERVER_KEY not in json.dumps(error)
    assert SUPABASE_URL not in json.dumps(error)


def test_roster_storage_rejects_public_key_for_server_only_reads_and_writes():
    with patch("supabase_history.requests.post") as mocked_post:
        write_result = supabase_history.write_roster_snapshot(
            roster_row(),
            supabase_url=SUPABASE_URL,
            api_key=PUBLIC_KEY,
        )
    with patch("supabase_history.requests.get") as mocked_get:
        read_result = supabase_history.read_roster_snapshots(
            CLAN_A,
            supabase_url=SUPABASE_URL,
            api_key=PUBLIC_KEY,
        )

    assert write_result["error"] == "server_key_required"
    assert read_result["error"] == "server_key_required"
    mocked_post.assert_not_called()
    mocked_get.assert_not_called()


def test_roster_storage_marks_old_history_stale():
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    with patch("supabase_history.requests.get") as mocked_get:
        mocked_get.return_value = storage_response(
            200,
            [roster_row(captured=old)],
        )
        result = supabase_history.read_roster_snapshots(
            CLAN_A,
            stale_after_seconds=60,
            supabase_url=SUPABASE_URL,
            api_key=SERVER_KEY,
        )

    assert result["status"] == "stale"
    assert result["data_status"] == "stale"
    assert result["stale"] is True


def test_first_last_seen_join_interval_and_role_changes_are_observed_only():
    t1 = "2026-08-01T12:00:00Z"
    t2 = "2026-08-02T12:00:00Z"
    history = join_data.calculate_roster_history(
        [
            roster_row(player="PLAYER1", captured=t1, role="member"),
            roster_row(player="PLAYER1", captured=t2, role="elder", name="Alice new"),
            roster_row(player="PLAYER2", captured=t2, role="member", name="Bob"),
        ],
        CLAN_A,
    )

    player1 = next(row for row in history["players"] if row["player_tag"] == "PLAYER1")
    assert player1["first_seen_in_clan"] == t1
    assert player1["last_seen_in_clan"] == t2
    assert player1["role_changes"][0]["from"] == "member"
    assert player1["role_changes"][0]["to"] == "elder"
    assert player1["role_changes"][0]["observed_between_snapshots"] == {
        "from": t1,
        "to": t2,
        "label": join_data.OBSERVED_INTERVAL_LABEL,
    }

    join = history["joins"][0]
    assert join["player_tag"] == "PLAYER2"
    assert join["first_seen_in_clan"] == t2
    assert join["observed_join_interval"] == {
        "from": t1,
        "to": t2,
        "label": join_data.OBSERVED_INTERVAL_LABEL,
    }
    assert join["ago"] == join_data.OBSERVED_INTERVAL_LABEL
    assert "exact" not in json.dumps(join).lower()


def test_one_missing_latest_snapshot_is_not_a_confirmed_leave():
    t1 = "2026-08-01T12:00:00Z"
    t2 = "2026-08-02T12:00:00Z"
    history = join_data.calculate_roster_history(
        [roster_row(captured=t1), roster_row(player="PLAYER2", captured=t2)],
        CLAN_A,
    )

    missing = history["missing_from_last_snapshot"]
    assert [row["player_tag"] for row in missing] == ["PLAYER1"]
    assert missing[0]["leave_status"] == "not_present_in_last_snapshot"
    assert missing[0]["confirmed_leave"] is False
    assert history["confirmed_leaves"] == []
    assert history["observed_leaves"][0]["observed_leave_interval"] == {
        "from": t1,
        "to": t2,
        "label": join_data.OBSERVED_INTERVAL_LABEL,
    }


def test_two_consecutive_absent_snapshots_confirm_an_observed_leave():
    t1 = "2026-08-01T12:00:00Z"
    t2 = "2026-08-02T12:00:00Z"
    t3 = "2026-08-03T12:00:00Z"
    history = join_data.calculate_roster_history(
        [
            roster_row(captured=t1),
            roster_row(player="PLAYER2", captured=t2),
            roster_row(player="PLAYER2", captured=t3),
        ],
        CLAN_A,
    )

    assert history["missing_from_last_snapshot"][0]["confirmed_leave"] is True
    assert history["confirmed_leaves"][0]["player_tag"] == "PLAYER1"
    assert history["confirmed_leaves"][0]["leave_status"] == "confirmed_observed_leave"
    assert history["confirmed_leaves"][0]["observed_leave_interval"]["from"] == t1
    assert history["confirmed_leaves"][0]["observed_leave_interval"]["to"] == t2


def test_collect_uses_persistent_rows_across_restart_and_does_not_call_first_snapshot_a_join():
    stored_rows = []
    read_results = [
        {"status": "empty", "data_status": "empty", "snapshots": []},
        {"status": "fresh", "data_status": "fresh", "snapshots": stored_rows},
    ]
    written_rows = []

    def fake_read(*args, **kwargs):
        return read_results.pop(0)

    def fake_write(rows, **kwargs):
        written_rows.extend(rows)
        stored_rows.extend(rows)
        return {"ok": True, "status": "ok", "data_status": "ok", "rows_written": len(rows)}

    with patch.dict(os.environ, {"CLASH_ROYALE_API_KEY": "clash-test-key"}, clear=False), patch(
        "api.join_data.api_get",
        side_effect=[
            member_payload(("#PLAYER1", "Alice", "member", 6000)),
            member_payload(
                ("#PLAYER1", "Alice", "elder", 6100),
                ("#PLAYER2", "Bob", "member", 5000),
            ),
        ],
    ), patch("api.join_data.read_roster_snapshots", side_effect=fake_read), patch(
        "api.join_data.write_roster_snapshots", side_effect=fake_write
    ):
        first = join_data.collect_join_data(10, CLAN_A)
        second = join_data.collect_join_data(10, CLAN_A)

    assert first["joins"] == []
    assert second["joins"][0]["player_tag"] == "PLAYER2"
    assert second["joins"][0]["observed_join_interval"]["label"] == join_data.OBSERVED_INTERVAL_LABEL
    assert second["role_changes"][0]["player_tag"] == "PLAYER1"
    assert len(written_rows) == 3
    assert len({(row["clan_tag"], row["player_tag"], row["captured_at"]) for row in written_rows}) == 3


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        ({"items": []}, "empty"),
        ({}, "unknown"),
        ({"items": [{"name": "No tag"}]}, "partial"),
        ({"items": "not-a-list"}, "partial"),
    ],
)
def test_collect_marks_missing_empty_and_partial_roster_data_without_writes(
    payload,
    expected_status,
):
    with patch.dict(os.environ, {"CLASH_ROYALE_API_KEY": "clash-test-key"}, clear=False), patch(
        "api.join_data.api_get", return_value=payload
    ), patch("api.join_data.read_roster_snapshots") as mocked_read, patch(
        "api.join_data.write_roster_snapshots"
    ) as mocked_write:
        result = join_data.collect_join_data(10, CLAN_A)

    assert result["roster_status"] == expected_status
    assert result["joins"] == []
    assert result["leaves"] == []
    if expected_status == "empty":
        assert result["missing_fields"] == []
    else:
        assert result["missing_fields"]
    mocked_read.assert_not_called()
    mocked_write.assert_not_called()


def test_collect_storage_failure_is_explicit_and_never_fabricates_a_transition():
    payload = member_payload(("#PLAYER1", "Alice", "member", 6000))
    with patch.dict(os.environ, {"CLASH_ROYALE_API_KEY": "clash-test-key"}, clear=False), patch(
        "api.join_data.api_get", return_value=payload
    ), patch(
        "api.join_data.read_roster_snapshots",
        return_value={"status": "error", "error": "upstream_server_error", "snapshots": None},
    ), patch(
        "api.join_data.write_roster_snapshots",
        return_value={"status": "error", "error": "upstream_server_error"},
    ):
        result = join_data.collect_join_data(10, CLAN_A)

    assert result["roster_status"] == "error"
    assert result["joins"] == []
    assert result["leaves"] == []
    assert result["storage"]["read"]["error"] == "upstream_server_error"
    assert result["storage"]["write"]["error"] == "upstream_server_error"
    assert "clash-test-key" not in json.dumps(result)


def test_join_route_error_response_is_safe_and_keeps_join_html_contract():
    request = type("Request", (), {})()
    request.path = "/api/join_data?clan=%239YP8UY"
    request.wfile = io.BytesIO()
    request.sent_status = None
    request.sent_headers = []
    request.send_response = lambda status: setattr(request, "sent_status", status)
    request.send_header = lambda key, value: request.sent_headers.append((key, value))
    request.end_headers = lambda: None

    with patch.object(
        join_data,
        "collect_join_data_official",
        side_effect=RuntimeError("request leaked admin-key-123"),
    ):
        join_data.handler.do_GET(request)

    body = request.wfile.getvalue().decode("utf-8")
    assert request.sent_status == 500
    assert "admin-key-123" not in body
    assert "details" not in body
    assert json.loads(body) == {
        "ok": False,
        "error": "Join data is temporarily unavailable.",
    }

    html = (Path(__file__).parents[1] / "join.html").read_text(encoding="utf-8")
    assert "/api/join_data?clan=" in html
    assert "data.joins" in html
    assert "row.name" in html
    assert "row.ago" in html


def test_join_route_rejects_unconfigured_clan_before_fetching_upstream():
    request = type("Request", (), {})()
    request.path = "/api/join_data?clan=NOT_A_CONFIGURED_CLAN"
    request.wfile = io.BytesIO()
    request.sent_status = None
    request.send_response = lambda status: setattr(request, "sent_status", status)
    request.send_header = lambda key, value: None
    request.end_headers = lambda: None

    with patch.object(join_data, "collect_join_data_official") as mocked_collect:
        join_data.handler.do_GET(request)

    assert request.sent_status == 400
    assert json.loads(request.wfile.getvalue()) == {
        "ok": False,
        "error": "Invalid clan tag.",
    }
    mocked_collect.assert_not_called()


def test_roster_migration_has_required_fields_natural_key_and_server_only_rls():
    sql = (
        Path(__file__).parents[1]
        / "supabase"
        / "migrations"
        / "20260827220000_clan_roster_snapshots.sql"
    ).read_text(encoding="utf-8").lower()

    for marker in (
        "create table public.clan_roster_snapshots",
        "clan_tag text not null",
        "player_tag text not null",
        "player_name text not null",
        "role text not null",
        "trophies integer",
        "seen_at timestamptz not null",
        "captured_at timestamptz not null",
        "unique (clan_tag, player_tag, captured_at)",
        "alter table public.clan_roster_snapshots enable row level security",
        "revoke all on table public.clan_roster_snapshots from public",
        "grant select, insert, update, delete",
        "to service_role",
    ):
        assert marker in sql
    assert "to anon" not in sql
    assert "to authenticated" not in sql
