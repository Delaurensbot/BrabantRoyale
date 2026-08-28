from api.reporting import (
    collect_day1_high_famers,
    parse_day_number,
    render_day4_last_chance_players,
    render_player_table,
)


def test_official_report_context_uses_explicit_day_metadata():
    assert parse_day_number({"day": 4}) == 4
    assert parse_day_number({"period_index": 2}) == 2
    assert parse_day_number({"day": None}) is None


def test_official_player_rows_render_missing_metrics_without_zero_defaults():
    text = render_player_table(
        [
            {
                "rank": 1,
                "name": "Alice",
                "role": "Member",
                "decks_used_today": None,
                "decks_total_so_far": None,
                "boat_attacks": None,
                "fame": None,
            }
        ]
    )

    assert "Medals" in text
    assert "Fame" not in text
    assert "0/0" not in text


def test_official_day_cards_are_driven_by_race_metadata_only():
    rows = [
        {
            "name": "Alice",
            "fame": 900,
            "decks_used_today": 0,
        },
    ]

    assert collect_day1_high_famers({"day": 1}, rows) == [("Alice", 900)]
    assert "Bob" in render_day4_last_chance_players(
        {"day": 4},
        [{"name": "Bob", "fame": 3000, "decks_used_today": 0}],
    )
