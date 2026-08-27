from pathlib import Path
import re


MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "supabase"
    / "migrations"
    / "20260827180000_duel_monitoring_live.sql"
)
SQL = MIGRATION_PATH.read_text(encoding="utf-8").lower()


def table_definition(table_name: str) -> str:
    match = re.search(
        rf"create\s+table\s+public\.{table_name}\s*\(",
        SQL,
    )
    assert match, f"missing table definition: {table_name}"

    depth = 1
    position = match.end()
    while depth and position < len(SQL):
        if SQL[position] == "(":
            depth += 1
        elif SQL[position] == ")":
            depth -= 1
        position += 1
    assert depth == 0, f"unbalanced table definition: {table_name}"
    return SQL[match.end() : position - 1]


def view_definition(view_name: str) -> str:
    match = re.search(
        rf"create\s+view\s+public\.{view_name}.*?\bas\s+(select.*?;)",
        SQL,
        flags=re.DOTALL,
    )
    assert match, f"missing view definition: {view_name}"
    return match.group(1)


def test_t05_tables_contain_the_requested_columns_and_storage_types():
    expected = {
        "river_race_live_snapshots": {
            "id bigint generated always as identity primary key",
            "clan_tag text not null",
            "season_id bigint not null",
            "section_index integer",
            "period_index integer not null",
            "period_type text not null",
            "race_created_at timestamptz not null",
            "player_tag text not null",
            "player_name text not null",
            "player_role text not null",
            "decks_used smallint",
            "decks_used_today smallint",
            "fame integer",
            "repair_points integer",
            "boat_attacks smallint",
            "boat_attacks_today smallint",
            "boat_defenses smallint",
            "boat_defenses_today smallint",
            "captured_at timestamptz not null",
            "source text not null",
            "payload_version integer not null",
            "capture_bucket timestamptz generated always as",
        },
        "war_player_day_events": {
            "id bigint generated always as identity primary key",
            "clan_tag text not null",
            "race_created_at timestamptz not null",
            "period_index integer not null",
            "player_tag text not null",
            "event_type text not null",
            "observed_decks_used_today smallint",
            "confidence text not null",
            "observed_at timestamptz not null",
            "details jsonb not null",
            "created_at timestamptz not null",
        },
        "notification_log": {
            "id bigint generated always as identity primary key",
            "event_key text not null",
            "channel text not null",
            "status text not null",
            "response_code integer",
            "sent_at timestamptz",
            "details jsonb not null",
        },
    }

    for table_name, columns in expected.items():
        definition = table_definition(table_name)
        for column in columns:
            assert column in definition, f"{table_name}: missing {column}"


def test_t05_capture_bucket_and_idempotency_keys_are_explicit():
    assert "interval '10 minutes'" in SQL
    assert "date_bin(" in SQL
    assert (
        "unique (\n"
        "      clan_tag,\n"
        "      race_created_at,\n"
        "      period_index,\n"
        "      player_tag,\n"
        "      capture_bucket\n"
        "    )"
    ) in SQL
    assert (
        "unique (\n"
        "      clan_tag,\n"
        "      race_created_at,\n"
        "      period_index,\n"
        "      player_tag,\n"
        "      event_type\n"
        "    )"
    ) in SQL
    assert "unique (event_key, channel)" in SQL


def test_t05_indexes_cover_clan_race_player_and_observation_reads():
    expected_indexes = {
        "river_race_live_snapshots_clan_race_capture_idx",
        "river_race_live_snapshots_player_history_idx",
        "war_player_day_events_clan_race_observed_idx",
        "war_player_day_events_player_observed_idx",
        "notification_log_channel_sent_idx",
        "notification_log_status_sent_idx",
    }
    for index_name in expected_indexes:
        assert f"create index {index_name}" in SQL

    assert (
        "on public.war_player_day_events (player_tag, observed_at desc)"
        in SQL
    )
    assert (
        "race_created_at desc,\n"
        "    period_index,\n"
        "    player_tag,\n"
        "    observed_at desc"
        in SQL
    )


def test_t05_raw_tables_are_rls_enabled_and_server_only():
    raw_tables = (
        "river_race_live_snapshots",
        "war_player_day_events",
        "notification_log",
    )
    for table_name in raw_tables:
        assert (
            f"alter table public.{table_name} enable row level security;"
            in SQL
        )
        assert f"revoke all on table public.{table_name} from public;" in SQL
        assert (
            f"grant select, insert, update, delete\n"
            f"  on table public.{table_name}\n"
            f"  to service_role;"
        ) in SQL
        assert re.search(
            rf"create policy .*?on public\.{table_name}.*?\bto service_role\b",
            SQL,
            flags=re.DOTALL,
        )
        assert not re.search(
            rf"grant [^;]*?on table public\.{table_name}[^;]*?\bto\s+(?:anon|authenticated)\b",
            SQL,
            flags=re.DOTALL,
        )


def test_t05_public_access_is_limited_to_aggregate_snapshot_summary():
    summary = view_definition("river_race_live_snapshot_summary")
    forbidden_public_fields = (
        "player_tag",
        "player_name",
        "player_role",
        "event_type",
        "confidence",
        "details",
        "observed_decks_used_today",
        "event_key",
        "response_code",
    )
    for field in forbidden_public_fields:
        assert re.search(rf"\b{field}\b", summary) is None

    assert (
        "grant select on public.river_race_live_snapshot_summary"
        " to anon, authenticated;"
    ) in SQL
    assert (
        "revoke all on public.river_race_live_snapshot_summary from public;"
        in SQL
    )


def test_t05_does_not_add_secrets_or_mutate_existing_history_objects():
    assert "sha256" not in SQL
    assert "secret" not in SQL
    assert "clan_war_player_weeks" not in SQL
    assert "clan_war_week_exclusions" not in SQL
