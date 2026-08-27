"""Public clan-policy read route and admin-key protected policy writes.

``GET /api/clan_policy?clan=<tag>`` returns only the validated, non-personal
policy model.  ``POST``/``PUT``/``PATCH`` replace that clan's policy and use
the existing ``X-Analytics-Admin-Key`` RLS boundary.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler
import json
from typing import Mapping, Optional
from urllib.parse import parse_qs, urlparse

try:
    from clan_policy import (
        ADMIN_KEY_HEADER,
        ClanPolicyStorageError,
        public_policy_payload,
        read_clan_policy,
        validate_clan_tag,
        write_clan_policy,
    )
except ImportError:  # pragma: no cover - convenient for loose Vercel files.
    from ..clan_policy import (  # type: ignore
        ADMIN_KEY_HEADER,
        ClanPolicyStorageError,
        public_policy_payload,
        read_clan_policy,
        validate_clan_tag,
        write_clan_policy,
    )

try:
    from Royale_api import DEFAULT_CLAN_TAG
except ImportError:  # pragma: no cover - convenient for package-style loading.
    from ..Royale_api import DEFAULT_CLAN_TAG


HTTP_STATUS_BAD_REQUEST = 400
HTTP_STATUS_FORBIDDEN = 403
HTTP_STATUS_BAD_GATEWAY = 502
HTTP_STATUS_METHOD_NOT_ALLOWED = 405
HTTP_STATUS_OK = 200
MAX_BODY_BYTES = 16 * 1024


def _requested_clan(path: str, *, body: Optional[Mapping[str, object]] = None) -> str:
    try:
        values = parse_qs(urlparse(path or "").query, keep_blank_values=True).get(
            "clan",
            [],
        )
    except (TypeError, ValueError):
        raise ValueError("Invalid clan tag.") from None
    if len(values) > 1:
        raise ValueError("Invalid clan tag.")
    supplied = values[0] if values and values[0].strip() else None
    if supplied is None and isinstance(body, Mapping):
        supplied = body.get("clan_tag")  # type: ignore[assignment]
    return validate_clan_tag(supplied or DEFAULT_CLAN_TAG)


def _read_body(request: BaseHTTPRequestHandler) -> Mapping[str, object]:
    try:
        content_length = int(request.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        raise ValueError("Invalid request size.") from None
    if content_length <= 0 or content_length > MAX_BODY_BYTES:
        raise ValueError("Invalid request size.")
    try:
        payload = json.loads(request.rfile.read(content_length) or b"{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("Request must contain valid JSON.") from None
    if not isinstance(payload, Mapping):
        raise ValueError("Request must be a JSON object.")
    return payload


def _safe_error_payload(error: str) -> dict[str, object]:
    return {"ok": False, "error": error}


class handler(BaseHTTPRequestHandler):
    """Vercel-compatible adapter for clan policy storage."""

    def _send_json(self, status_code: int, payload: Mapping[str, object]) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        try:
            clan_tag = _requested_clan(getattr(self, "path", ""))
            result = read_clan_policy(clan_tag)
            payload = public_policy_payload(result)
            status = HTTP_STATUS_OK if result.get("ok") else HTTP_STATUS_BAD_GATEWAY
            self._send_json(status, payload)
        except ValueError:
            self._send_json(
                HTTP_STATUS_BAD_REQUEST,
                _safe_error_payload("Invalid clan tag."),
            )
        except Exception:
            # Never serialize upstream/transport exception text: it can contain
            # request metadata or deployment secrets.
            self._send_json(
                HTTP_STATUS_BAD_GATEWAY,
                _safe_error_payload("Policy data is temporarily unavailable."),
            )

    def _write(self) -> None:
        admin_key = self.headers.get(ADMIN_KEY_HEADER, "")
        if not isinstance(admin_key, str) or not admin_key.strip():
            self._send_json(
                HTTP_STATUS_FORBIDDEN,
                _safe_error_payload("Unauthorized."),
            )
            return
        try:
            payload = _read_body(self)
            clan_tag = _requested_clan(getattr(self, "path", ""), body=payload)
            request_payload = dict(payload)
            request_payload["clan_tag"] = clan_tag
            result = write_clan_policy(request_payload, admin_key.strip())
            self._send_json(HTTP_STATUS_OK, public_policy_payload(result))
        except PermissionError:
            self._send_json(
                HTTP_STATUS_FORBIDDEN,
                _safe_error_payload("Unauthorized."),
            )
        except ValueError:
            self._send_json(
                HTTP_STATUS_BAD_REQUEST,
                _safe_error_payload("Invalid policy request."),
            )
        except ClanPolicyStorageError as error:
            status = (
                HTTP_STATUS_FORBIDDEN
                if error.code == "forbidden"
                else HTTP_STATUS_BAD_GATEWAY
            )
            self._send_json(
                status,
                _safe_error_payload(
                    "Unauthorized."
                    if error.code == "forbidden"
                    else "Policy storage is temporarily unavailable."
                ),
            )
        except Exception:
            self._send_json(
                HTTP_STATUS_BAD_GATEWAY,
                _safe_error_payload("Policy storage is temporarily unavailable."),
            )

    def do_POST(self) -> None:
        handler._write(self)

    def do_PUT(self) -> None:
        handler._write(self)

    def do_PATCH(self) -> None:
        handler._write(self)

    def do_DELETE(self) -> None:
        self._send_json(
            HTTP_STATUS_METHOD_NOT_ALLOWED,
            _safe_error_payload("Method not allowed."),
        )


__all__ = [
    "ADMIN_KEY_HEADER",
    "HTTP_STATUS_BAD_GATEWAY",
    "HTTP_STATUS_BAD_REQUEST",
    "HTTP_STATUS_FORBIDDEN",
    "HTTP_STATUS_METHOD_NOT_ALLOWED",
    "HTTP_STATUS_OK",
    "handler",
]
