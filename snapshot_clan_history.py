#!/usr/bin/env python3
"""Store recent river-race data in Supabase for long-term analytics."""

import argparse
import json
import os
import sys

from Royale_api import CLAN_CONFIGS, get_clan_config
from supabase_history import get_supabase_write_config, normalize_tag, snapshot_clan


def configured_clans(raw_tags: str):
    requested = [
        normalize_tag(value)
        for value in raw_tags.split(",")
        if normalize_tag(value)
    ]
    tags = requested or list(CLAN_CONFIGS.keys())
    return [get_clan_config(tag) for tag in tags]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--clans",
        default=os.environ.get("CLAN_TAGS", ""),
        help="Comma-separated clan tags. Defaults to all configured clans.",
    )
    args = parser.parse_args()

    clash_api_key = os.environ.get("CLASH_ROYALE_API_KEY", "").strip()
    if not clash_api_key:
        print("Missing CLASH_ROYALE_API_KEY environment variable.", file=sys.stderr)
        return 2

    try:
        (
            supabase_url,
            supabase_api_key,
            supabase_ingest_token,
        ) = get_supabase_write_config()
        results = [
            snapshot_clan(
                clan["tag"],
                clan["name"],
                clash_api_key=clash_api_key,
                supabase_url=supabase_url,
                supabase_api_key=supabase_api_key,
                supabase_ingest_token=supabase_ingest_token,
            )
            for clan in configured_clans(args.clans)
        ]
    except Exception as exc:
        print(f"History snapshot failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"ok": True, "clans": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
