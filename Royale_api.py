"""Compatibility exports for official-data reporting helpers.

The former module fetched and parsed third-party HTML. That implementation
was removed in T17. Production routes import :mod:`api.config` and
:mod:`api.reporting` directly; this small facade keeps existing pure helper
imports working without retaining the removed legacy acquisition path.
"""

from api.config import (
    CLAN_CONFIGS,
    DEFAULT_CLAN_CONFIG,
    DEFAULT_CLAN_TAG,
    OUR_CLAN_NAME_DEFAULT,
    get_clan_config,
    normalize_tag,
)
from api.reporting import (
    ClanOverview,
    attacks_left_today,
    bucket_open_players,
    build_short_story,
    clean_text,
    collect_day1_high_famers,
    compute_battles_left,
    compute_duels_left,
    compute_total_players_participated,
    find_our_clan,
    get_projected_ranking,
    parse_day_label,
    parse_day_number,
    render_battles_left_today,
    render_clan_avg_projection,
    render_clan_insights,
    render_clan_overview_table,
    render_clan_stats_block,
    render_day1_high_fame_players,
    render_day4_last_chance_players,
    render_high_fame_players,
    render_player_table,
    render_risk_left_attacks,
    translate_day_label,
)

__all__ = [
    "CLAN_CONFIGS",
    "ClanOverview",
    "DEFAULT_CLAN_CONFIG",
    "DEFAULT_CLAN_TAG",
    "OUR_CLAN_NAME_DEFAULT",
    "attacks_left_today",
    "bucket_open_players",
    "build_short_story",
    "clean_text",
    "collect_day1_high_famers",
    "compute_battles_left",
    "compute_duels_left",
    "compute_total_players_participated",
    "find_our_clan",
    "get_clan_config",
    "get_projected_ranking",
    "normalize_tag",
    "parse_day_label",
    "parse_day_number",
    "render_battles_left_today",
    "render_clan_avg_projection",
    "render_clan_insights",
    "render_clan_overview_table",
    "render_clan_stats_block",
    "render_day1_high_fame_players",
    "render_day4_last_chance_players",
    "render_high_fame_players",
    "render_player_table",
    "render_risk_left_attacks",
    "translate_day_label",
]
