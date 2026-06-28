from api.cwstats import (
    parse_cwstats_finish_outlook_from_html,
    parse_cwstats_race_context_from_html,
)


def test_parse_cwstats_race_context_maps_boat_and_medals_in_correct_order():
    html = """
    <html><body>
      <a href="/clan/ABC123/race">
        3 Brabant Royale 3530 9550 3435 170.54
      </a>
    </body></html>
    """

    parsed = parse_cwstats_race_context_from_html(html)
    row = parsed["rows_by_name"]["brabantroyale"]

    assert row["trophy"] == 3530
    assert row["boat_movement"] == 9550
    assert row["cw_trophy"] == 3435

def test_parse_cwstats_race_context_fallback_for_updated_layout():
    html = """
    <html><body>
      <div>
        1 #1 Clan 4,422 Clan War trophies 0 Boat movement 11,650 Fame 176.52
        4 Brabant Royale 4,330 Clan War trophies 0 Boat movement 7,350 Fame 179.27
      </div>
    </body></html>
    """

    parsed = parse_cwstats_race_context_from_html(html)
    row = parsed["rows_by_name"]["brabantroyale"]

    assert row["rank"] == 4
    assert row["trophy"] == 7350
    assert row["boat_movement"] == 0
    assert row["cw_trophy"] == 4330
    assert row["fame_avg"] == 179.27


def test_parse_cwstats_race_context_from_embedded_json_payload():
    html = """
    <html><body>
      <script id="__NEXT_DATA__" type="application/json">{
        "props": {
          "pageProps": {
            "race": {
              "rows": [
                {
                  "rank": 4,
                  "name": "Brabant Royale",
                  "clanWarTrophies": 4330,
                  "boatMovement": 0,
                  "fame": 7350,
                  "fameAvg": 179.27
                }
              ]
            }
          }
        }
      }</script>
    </body></html>
    """

    parsed = parse_cwstats_race_context_from_html(html)
    row = parsed["rows_by_name"]["brabantroyale"]

    assert row["rank"] == 4
    assert row["cw_trophy"] == 4330
    assert row["boat_movement"] == 0
    assert row["trophy"] == 7350
    assert row["fame_avg"] == 179.27


def test_parse_cwstats_race_context_from_current_link_layout():
    html = """
    <html><body>
      <a href="/clan/9YP8UY/race">
        1 Brabant Royale 4.3K 4,320 9,105 20,450 157.31
      </a>
    </body></html>
    """

    parsed = parse_cwstats_race_context_from_html(html)
    row = parsed["rows_by_name"]["brabantroyale"]

    assert row["rank"] == 1
    assert row["clan_war_trophies"] == 4320
    assert row["boat_movement"] == 9105
    assert row["fame"] == 20450
    assert row["fame_avg"] == 157.31


def test_parse_cwstats_finish_outlook_from_current_layout():
    html = """
    <html><body>
      Race Outlook Today Decks used 130 / 200 Slots used 34 / 50
      Possible Finish Best possible 1st 36,050 Worst possible 5th 27,450
      Projected Finish Placement 5th 31,350 To 4th Place 150 los mancos
    </body></html>
    """

    parsed = parse_cwstats_finish_outlook_from_html(html)

    assert parsed["battles_left"] == 70
    assert parsed["duels_left"] == 16
    assert parsed["best_rank"] == 1
    assert parsed["best_finish"] == 36050
    assert parsed["worst_rank"] == 5
    assert parsed["worst_finish"] == 27450
    assert parsed["projected_rank"] == 5
    assert parsed["projected_finish"] == 31350
