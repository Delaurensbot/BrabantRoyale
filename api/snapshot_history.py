from http.server import BaseHTTPRequestHandler
import hashlib
import hmac
import json
import os

try:
    from api.config import CLAN_CONFIGS, get_clan_config
except ImportError:  # pragma: no cover - useful when loaded as a loose file.
    from config import CLAN_CONFIGS, get_clan_config
from supabase_history import (
    DEFAULT_SUPABASE_PUBLISHABLE_KEY,
    DEFAULT_SUPABASE_URL,
    snapshot_clan,
)


INGEST_TOKEN_SHA256 = (
    "4081cda4c915d344411da8a21ac324b77877f28fed623e01705a5f114bdf0c36"
)


def token_matches(token: str, expected_hash: str = INGEST_TOKEN_SHA256) -> bool:
    actual_hash = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
    return bool(token) and hmac.compare_digest(actual_hash, expected_hash)


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        ingest_token = self.headers.get("X-Ingest-Token", "")
        if not token_matches(ingest_token):
            self._send_json(401, {"ok": False, "error": "Unauthorized"})
            return

        clash_api_key = os.environ.get("CLASH_ROYALE_API_KEY", "").strip()
        if not clash_api_key:
            self._send_json(
                500,
                {"ok": False, "error": "CLASH_ROYALE_API_KEY is not configured"},
            )
            return

        supabase_url = (
            os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
            or DEFAULT_SUPABASE_URL
        )
        supabase_api_key = (
            os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
            or DEFAULT_SUPABASE_PUBLISHABLE_KEY
        )

        try:
            results = []
            for clan_tag in CLAN_CONFIGS:
                clan = get_clan_config(clan_tag)
                results.append(
                    snapshot_clan(
                        clan["tag"],
                        clan["name"],
                        clash_api_key=clash_api_key,
                        supabase_url=supabase_url,
                        supabase_api_key=supabase_api_key,
                        supabase_ingest_token=ingest_token,
                    )
                )
            self._send_json(200, {"ok": True, "clans": results})
        except Exception:
            self._send_json(
                500,
                {"ok": False, "error": "Snapshot history kon niet worden bijgewerkt."},
            )

    def do_GET(self):
        self._send_json(405, {"ok": False, "error": "Method not allowed"})
