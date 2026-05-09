from api.cwstats import parse_cwstats_players_from_html


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
    assert players[1]["decks_used_today"] == 0
    assert players[1]["decks_total_so_far"] == 4
