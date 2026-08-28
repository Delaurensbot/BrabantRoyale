"""Configured clans and official API routing metadata.

This module is the single configuration seam for clan identity. It contains
no HTML URLs and deliberately does not perform network I/O; upstream reads are
owned by :mod:`api.clash_client`.
"""

from __future__ import annotations

from typing import Dict, Optional

try:
    from api.clash_client import (
        ROYAL_API_BASE_URL,
        ClashClientError,
        normalize_tag,
    )
except ImportError:  # pragma: no cover - useful when loaded as a loose file.
    from clash_client import ROYAL_API_BASE_URL, ClashClientError, normalize_tag


DEFAULT_CLAN_TAG = "9YP8UY"
CLAN_CONFIGS = {
    DEFAULT_CLAN_TAG: {"name": "Brabant Royale"},
    "GPCLVLPP": {"name": "Brabant Royale 2"},
    "RLQQQC99": {"name": "Brabant Royale 3"},
}

DEFAULT_CLAN_NAME = CLAN_CONFIGS[DEFAULT_CLAN_TAG]["name"]
OUR_CLAN_NAME_DEFAULT = DEFAULT_CLAN_NAME


def get_clan_config(tag: Optional[str] = None) -> Dict[str, str]:
    """Return canonical identity and official endpoint paths for ``tag``.

    Empty values retain the historical default-clan behavior. A syntactically
    valid but unconfigured tag is kept as-is so callers that perform their own
    allow-list validation do not accidentally inspect the default clan.
    """

    raw_tag = tag if isinstance(tag, str) and tag.strip() else DEFAULT_CLAN_TAG
    try:
        normalized = normalize_tag(raw_tag)
    except ClashClientError:
        normalized = DEFAULT_CLAN_TAG

    configured = CLAN_CONFIGS.get(normalized, {})
    encoded = f"%23{normalized}"
    return {
        "tag": normalized,
        "name": str(configured.get("name") or ""),
        "official_api_base_url": ROYAL_API_BASE_URL,
        "clan_path": f"/clans/{encoded}",
        "members_path": f"/clans/{encoded}/members",
        "race_path": f"/clans/{encoded}/currentriverrace",
        "race_log_path": f"/clans/{encoded}/riverracelog",
        "player_path": "/players/%23{player_tag}",
    }


DEFAULT_CLAN_CONFIG = get_clan_config(DEFAULT_CLAN_TAG)


__all__ = [
    "CLAN_CONFIGS",
    "DEFAULT_CLAN_CONFIG",
    "DEFAULT_CLAN_NAME",
    "DEFAULT_CLAN_TAG",
    "OUR_CLAN_NAME_DEFAULT",
    "get_clan_config",
    "normalize_tag",
]
