from http.server import BaseHTTPRequestHandler
import json
import os

import requests


ROYAL_API_BASE_URL = "https://proxy.royaleapi.dev/v1"
CLAN_TAG_ENCODED = "%239YP8UY"  # '#9YP8UY' must be URL-encoded in the request path.


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Keep the Clash Royale API key server-side only.
        api_key = os.environ.get("CLASH_ROYALE_API_KEY")
        if not api_key:
            self._send_json(
                500,
                {
                    "ok": False,
                    "error": "Missing CLASH_ROYALE_API_KEY environment variable.",
                },
            )
            return

        endpoint = f"{ROYAL_API_BASE_URL}/clans/{CLAN_TAG_ENCODED}/members"

        try:
            response = requests.get(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json() if response.content else {}

            # Only expose frontend-relevant data: a plain array of player names.
            names = [item.get("name") for item in payload.get("items", []) if item.get("name")]

            self._send_json(
                200,
                {
                    "ok": True,
                    "names": names,
                },
            )
        except requests.HTTPError as http_err:
            # Return a safe error message and status when the upstream API responds with an error.
            status = http_err.response.status_code if http_err.response is not None else 502
            self._send_json(
                status,
                {
                    "ok": False,
                    "error": f"Upstream API error ({status}).",
                },
            )
        except requests.RequestException:
            # Network/timeout issues while reaching RoyaleAPI proxy.
            self._send_json(
                502,
                {
                    "ok": False,
                    "error": "Failed to contact RoyaleAPI proxy.",
                },
            )
        except Exception:
            # Catch-all so the route always returns JSON.
            self._send_json(
                500,
                {
                    "ok": False,
                    "error": "Unexpected server error.",
                },
            )

    def _send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
