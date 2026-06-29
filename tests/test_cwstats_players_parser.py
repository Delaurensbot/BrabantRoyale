from api.cwstats import parse_cwstats_players_from_html
from Royale_api import render_player_table


def test_parse_cwstats_players_maps_used_today_column_correctly():
    html = """
    <table>
      <tr><th>#</th><th>Player</th><th>Boat movement</th><th>Cards used today</th><th>Cards</th><th>Fame</th></tr>
      <tr><td>1</td><td>mrpr</td><td>0</td><td>4</td><td>8</td><td>1000</td></tr>
      <tr><td>2</td><td>delaurens3</td><td>0</td><td>0</td><td>4</td><td>500</td></tr>
    </table>
    """
    players = parse_cwstats_players_from_html(html)

    assert players[0]["decks_used_today"] == 4
    assert players[0]["decks_total_so_far"] == 8
    assert players[0]["fame"] == 1000
    assert players[1]["decks_used_today"] == 0
    assert players[1]["decks_total_so_far"] == 4
    assert players[1]["fame"] == 500


def test_parse_cwstats_players_uses_medals_or_score_header_for_points():
    html = """
    <table>
      <tr><th>#</th><th>Player</th><th>Boat</th><th>Today</th><th>Total</th><th>Medals</th></tr>
      <tr><td>1</td><td>mrpr</td><td>0</td><td>3</td><td>7</td><td>1,234</td></tr>
    </table>
    """

    players = parse_cwstats_players_from_html(html)

    assert players[0]["decks_used_today"] == 3
    assert players[0]["decks_total_so_far"] == 7
    assert players[0]["fame"] == 1234


def test_render_player_table_labels_score_as_medals():
    text = render_player_table([
        {
            "rank": 1,
            "name": "mrpr",
            "role": "Member",
            "decks_used_today": 4,
            "decks_total_so_far": 8,
            "boat_attacks": 0,
            "fame": 1000,
        }
    ])

    assert "Medals" in text
    assert "Fame" not in text
    assert "1000" in text
