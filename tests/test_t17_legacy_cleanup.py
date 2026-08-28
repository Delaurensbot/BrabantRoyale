from pathlib import Path
from unittest.mock import patch

from api.player_summary import classify_error
from supabase_history import (
    load_history_races_from_env,
    load_week_exclusions_from_env,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_temporary_routes_and_scraper_modules_are_removed():
    for relative_path in (
        "cwstats_race.py",
        "Royale_api_join_data.py",
        "test-clan.html",
        "api/test-clan.py",
        "api/test-clan-prototype.py",
    ):
        assert not (REPOSITORY_ROOT / relative_path).exists()


def test_production_sources_have_no_html_scraper_or_prototype_markers():
    production_files = [
        *REPOSITORY_ROOT.glob("*.py"),
        *((REPOSITORY_ROOT / "api").glob("*.py")),
        REPOSITORY_ROOT / "index.html",
    ]
    forbidden_markers = (
        "BeautifulSoup",
        "bs4",
        "fetch_html",
        "/api/test-clan",
        "/test-clan.html",
    )

    for path in production_files:
        source = path.read_text(encoding="utf-8")
        assert not any(marker in source for marker in forbidden_markers), path


def test_official_config_is_the_only_route_source():
    config_source = (REPOSITORY_ROOT / "api" / "config.py").read_text(
        encoding="utf-8"
    )
    client_source = (REPOSITORY_ROOT / "api" / "clash_client.py").read_text(
        encoding="utf-8"
    )

    assert "ROYAL_API_BASE_URL" in config_source
    assert "official_api_base_url" in config_source
    assert "Authorization" in client_source
    assert "BeautifulSoup" not in config_source
    assert "BeautifulSoup" not in client_source


def test_public_error_surfaces_do_not_echo_upstream_details_or_secrets():
    status, message = classify_error(RuntimeError("upstream secret and URL"))
    assert status == 500
    assert "secret" not in message
    assert "URL" not in message

    with patch(
        "supabase_history.get_supabase_read_config",
        return_value=("https://supabase.example", "server-key"),
    ), patch(
        "supabase_history.fetch_history_rows",
        side_effect=RuntimeError("upstream secret and URL"),
    ):
        _, history_status = load_history_races_from_env("9YP8UY")

    with patch(
        "supabase_history.get_supabase_read_config",
        return_value=("https://supabase.example", "server-key"),
    ), patch(
        "supabase_history.fetch_week_exclusions",
        side_effect=RuntimeError("upstream secret and URL"),
    ):
        _, exclusion_status = load_week_exclusions_from_env("9YP8UY")

    for status_payload in (history_status, exclusion_status):
        assert "secret" not in str(status_payload)
        assert "supabase.example" not in str(status_payload)
