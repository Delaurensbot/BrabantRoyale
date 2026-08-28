from pathlib import Path
import re


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (REPOSITORY_ROOT / "index.html").read_text(encoding="utf-8")


def war_room_source() -> str:
    start = INDEX_HTML.index('  let warRoomCurrentData = null;')
    end = INDEX_HTML.index('  function parseOverviewValue', start)
    return INDEX_HTML[start:end]


def test_war_room_markup_covers_mobile_operational_surface():
    required_markers = (
        '<section id="warRoom"',
        'id="warRoomStatus"',
        'id="warRoomRefresh"',
        'id="warRoomSnapshot"',
        'id="warRoomObservedAt"',
        'id="warRoomSource"',
        'id="warRoomDataQuality"',
        'id="warRoomPhase"',
        'Wie moet nog?',
        'id="warRoomDecksRemaining"',
        'Duel-first-overzicht',
        'id="warRoomActionOnly"',
        'Alleen actie nodig',
        'id="warRoomBoatCard"',
        'Bootinformatie',
        'Opponent board',
        'class="warRoom__estimateLabel">schatting</span>',
    )

    for marker in required_markers:
        assert marker in INDEX_HTML


def test_war_room_uses_canonical_clan_and_t09_read_contract():
    source = war_room_source()

    assert "const requestedClan = currentClanTag;" in source
    assert re.search(
        r"`/api/war_status\?clan=\$\{encodeURIComponent\(requestedClan\)\}`",
        source,
    )
    assert '{ cache: "no-store" }' in source

    for field in (
        "race_context",
        "clan_rows",
        "player_rows",
        "duel_first_summary",
        "alerts",
        "freshness",
        "data_quality",
        "observed_at",
        "source",
        "status",
    ):
        assert field in source


def test_war_room_has_explicit_loading_live_stale_error_and_empty_states():
    source = war_room_source()

    for state in ("loading", "live", "stale", "error", "empty"):
        assert f'"{state}"' in source or f'--{state}' in INDEX_HTML
    assert 'data-state="loading"' in INDEX_HTML
    assert "warRoomSetState(\"loading\")" in source
    assert "warRoomRenderError" in source
    assert "warRoomRenderEmpty" in source
    assert "War Room wordt geladen" in INDEX_HTML
    assert "War Room kon niet worden ververst" in INDEX_HTML
    assert "Geen actieve race beschikbaar" in INDEX_HTML


def test_war_room_missing_values_are_visible_and_not_coerced_to_zero():
    source = war_room_source()

    assert 'return "—"' in source
    assert 'fallback = "onbekend"' in source
    assert "warRoomNumber(value)" in source
    assert "remaining == null" in source
    assert "Resterende decks: onbekend" in source
    assert "niet als nul geteld" in source


def test_war_room_duel_first_is_public_aggregate_only():
    source = war_room_source()

    assert "duel_first_summary" in source
    assert 'data?.alerts_scope !== "public_aggregate"' in source
    assert "alert.count" in source
    assert "alert.name" not in source
    assert "alert.player_tag" not in source
    assert "individuele Duel-first-statussen worden niet getoond" in INDEX_HTML
    assert "geen individuele overtredings- of beschuldigingslijst" in INDEX_HTML


def test_war_room_dynamic_upstream_values_use_text_content_without_new_html_injection():
    source = war_room_source()

    assert "document.createElement" in source
    assert "textContent" in source
    assert "innerHTML" not in source
    assert "warRoomClear" in source


def test_existing_dashboard_markers_remain_present():
    for marker in (
        'id="clanSwitch"',
        'id="refresh"',
        'id="overview"',
        'id="finishOutlook"',
        'id="players"',
        'id="strategy"',
        'const CLANS =',
        'const DEFAULT_CLAN = "9YP8UY"',
        "function fetchData()",
        "/api/cwstats?clan=",
        "function init()",
    ):
        assert marker in INDEX_HTML

    assert "/api/test-clan-prototype" not in INDEX_HTML
    assert "/test-clan.html" not in INDEX_HTML


def test_frontend_contains_no_server_credentials_or_private_headers():
    forbidden_tokens = (
        "CLASH_ROYALE_API_KEY",
        "SUPABASE_SECRET_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_ANON_KEY",
        "SUPABASE_PUBLISHABLE_KEY",
        "SUPABASE_INGEST_TOKEN",
        "WAR_STATUS_LEADER_SECRET",
        "X-War-Status-Leader-Secret",
        "WEBHOOK_SECRET",
        "webhooksecret",
        "leader-secret",
        "Authorization",
        "Bearer ",
    )

    for token in forbidden_tokens:
        assert token not in INDEX_HTML
