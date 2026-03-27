from api.cwstats import parse_cwstats_race_context_from_html


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
