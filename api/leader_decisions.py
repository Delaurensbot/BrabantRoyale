"""Admin-key protected leader-decision audit route.

The route intentionally has no public read mode.  Both ``GET`` and ``POST``
require the existing ``X-Analytics-Admin-Key`` header, and decision rows are
append-only in the T13 migration.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler
import json
from typing import Mapping, Optional
from urllib.parse import parse_qs, urlparse

try:
    from clan_policy import (
        ADMIN_KEY_HEADER,
        DEFAULT_DECISION_LIMIT,
        MAX_DECISION_LIMIT,
        ClanPolicyStorageError,
        read_leader_decisions,
        validate_clan_tag,
        write_leader_decision,
    )
except ImportError:  # pragma: no cover - convenient for loose Vercel files.
    from ..clan_policy import (  # type: ignore
        ADMIN_KEY_HEADER,
        DEFAULT_DECISION_LIMIT,
        MAX_DECISION_LIMIT,
        ClanPolicyStorageError,
        read_leader_decisions,
        validate_clan_tag,
        write_leader_decision,
    )

try:
    from api.config import DEFAULT_CLAN_TAG
except ImportError:  # pragma: no cover - convenient for loose-file loading.
    from config import DEFAULT_CLAN_TAG


HTTP_STATUS_BAD_REQUEST = 400
HTTP_STATUS_FORBIDDEN = 403
HTTP_STATUS_BAD_GATEWAY = 502
HTTP_STATUS_METHOD_NOT_ALLOWED = 405
HTTP_STATUS_OK = 200
MAX_BODY_BYTES = 16 * 1024


def _query_values(path: str, name: str) -> list[str]:
    try:
        return parse_qs(urlparse(path or "").query, keep_blank_values=True).get(
            name,
            [],
        )
    except (TypeError, ValueError):
        raise ValueError("Invalid request.") from None


def _requested_clan(path: str, *, body: Optional[Mapping[str, object]] = None) -> str:
    values = _query_values(path, "clan")
    if len(values) > 1:
        raise ValueError("Invalid clan tag.")
    supplied = values[0] if values and values[0].strip() else None
    if supplied is None and isinstance(body, Mapping):
        supplied = body.get("clan_tag")  # type: ignore[assignment]
    return validate_clan_tag(supplied or DEFAULT_CLAN_TAG)


def _requested_limit(path: str) -> int:
    values = _query_values(path, "limit")
    if not values or not values[0].strip():
        return DEFAULT_DECISION_LIMIT
    if len(values) != 1:
        raise ValueError("Invalid limit.")
    raw = values[0].strip()
    if not raw.isdigit():
        raise ValueError("Invalid limit.")
    value = int(raw)
    if value < 1 or value > MAX_DECISION_LIMIT:
        raise ValueError("Invalid limit.")
    return value


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
    """Vercel-compatible adapter for the private leader-decision log."""

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

    def _admin_key(self) -> str:
        value = self.headers.get(ADMIN_KEY_HEADER, "")
        return value.strip() if isinstance(value, str) else ""

    def do_GET(self) -> None:
        admin_key = handler._admin_key(self)
        if not admin_key:
            self._send_json(
                HTTP_STATUS_FORBIDDEN,
                _safe_error_payload("Unauthorized."),
            )
            return
        try:
            path = getattr(self, "path", "")
            clan_tag = _requested_clan(path)
            limit = _requested_limit(path)
            result = read_leader_decisions(clan_tag, admin_key, limit=limit)
            self._send_json(HTTP_STATUS_OK, result)
        except ValueError:
            self._send_json(
                HTTP_STATUS_BAD_REQUEST,
                _safe_error_payload("Invalid decision request."),
            )
        except PermissionError:
            self._send_json(
                HTTP_STATUS_FORBIDDEN,
                _safe_error_payload("Unauthorized."),
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
                    else "Decision storage is temporarily unavailable."
                ),
            )
        except Exception:
            self._send_json(
                HTTP_STATUS_BAD_GATEWAY,
                _safe_error_payload("Decision storage is temporarily unavailable."),
            )

    def do_POST(self) -> None:
        admin_key = handler._admin_key(self)
        if not admin_key:
            self._send_json(
                HTTP_STATUS_FORBIDDEN,
                _safe_error_payload("Unauthorized."),
            )
            return
        try:
            path = getattr(self, "path", "")
            payload = _read_body(self)
            clan_tag = _requested_clan(path, body=payload)
            request_payload = dict(payload)
            request_payload["clan_tag"] = clan_tag
            result = write_leader_decision(request_payload, admin_key)
            self._send_json(HTTP_STATUS_OK, result)
        except ValueError:
            self._send_json(
                HTTP_STATUS_BAD_REQUEST,
                _safe_error_payload("Invalid decision request."),
            )
        except PermissionError:
            self._send_json(
                HTTP_STATUS_FORBIDDEN,
                _safe_error_payload("Unauthorized."),
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
                    else "Decision storage is temporarily unavailable."
                ),
            )
        except Exception:
            self._send_json(
                HTTP_STATUS_BAD_GATEWAY,
                _safe_error_payload("Decision storage is temporarily unavailable."),
            )

    def do_PUT(self) -> None:
        self._send_json(
            HTTP_STATUS_METHOD_NOT_ALLOWED,
            _safe_error_payload("Method not allowed."),
        )

    def do_PATCH(self) -> None:
        self.do_PUT()

    def do_DELETE(self) -> None:
        self.do_PUT()


__all__ = [
    "ADMIN_KEY_HEADER",
    "HTTP_STATUS_BAD_GATEWAY",
    "HTTP_STATUS_BAD_REQUEST",
    "HTTP_STATUS_FORBIDDEN",
    "HTTP_STATUS_METHOD_NOT_ALLOWED",
    "HTTP_STATUS_OK",
    "handler",
]
