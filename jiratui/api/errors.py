"""Error types for the Jira client and mapping from HTTP/transport failures.

Every failure the UI can encounter is turned into a :class:`JiraError`
subclass with a human-readable ``message`` and an optional ``hint`` so that
screens can always show *something* actionable instead of spinning.

Jira error bodies look like ``{"errorMessages": [...], "errors": {"field": "msg"}}``
(core and Agile APIs alike). Jira also reports authentication problems in
headers: ``X-Seraph-LoginReason`` (``AUTHENTICATED_FAILED``,
``AUTHENTICATION_DENIED``) and ``X-Authentication-Denied-Reason`` (CAPTCHA).
"""

from __future__ import annotations

import re
import ssl
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..redact import redact

SERAPH_HEADER = "x-seraph-loginreason"
DENIED_HEADER = "x-authentication-denied-reason"


class JiraError(Exception):
    """Base error. ``message`` is user-facing; ``hint`` suggests a fix."""

    default_hint: str = ""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        hint: Optional[str] = None,
        error_messages: Optional[List[str]] = None,
        field_errors: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.hint = hint if hint is not None else self.default_hint
        self.error_messages = list(error_messages or [])
        self.field_errors = dict(field_errors or {})

    def describe(self) -> str:
        prefix = f"HTTP {self.status}: " if self.status else ""
        text = f"{prefix}{self.message}"
        if self.hint:
            text += f"\nHint: {self.hint}"
        return text

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.describe()


class ConfigError(JiraError):
    """Invalid user input (e.g. malformed URL) detected before any request."""


class AuthenticationError(JiraError):
    default_hint = (
        "The server rejected the credentials. Check the token/password, that the "
        "token has not expired, and that the auth method matches (Bearer PAT vs Basic)."
    )


class CaptchaRequiredError(AuthenticationError):
    default_hint = (
        "Jira requires a CAPTCHA after too many failed logins, and REST calls cannot "
        "answer it. Log in once in a browser (solving the CAPTCHA) to reset the counter, "
        "or use a personal access token (Bearer), which is not subject to CAPTCHA."
    )


class PermissionDeniedError(JiraError):
    default_hint = "Authenticated, but this account/token lacks permission for this resource."


class NotFoundError(JiraError):
    default_hint = "The resource does not exist or is not visible to this account."


class BadRequestError(JiraError):
    """HTTP 400: invalid JQL, invalid field values on create, etc."""

    default_hint = ""


class JqlError(BadRequestError):
    default_hint = "Fix the JQL and run it again (field names and values are case-insensitive)."


class ConflictError(JiraError):
    pass


class RateLimitedError(JiraError):
    default_hint = "Jira is rate limiting requests. Wait a moment and retry."


class ServerError(JiraError):
    default_hint = "The server returned an internal error. Retry, or check server health."


class UnexpectedResponseError(JiraError):
    default_hint = (
        "The response was not Jira REST JSON. Is the URL the Jira base URL "
        "(not a login/SSO page or a proxy)?"
    )


class NetworkError(JiraError):
    default_hint = "Check the URL, VPN connection and DNS. No proxy is used unless HTTPS_PROXY is set."


class TimeoutError_(NetworkError):  # noqa: N801 - avoid shadowing builtin
    default_hint = (
        "The server did not respond in time. Check VPN/network, or increase the "
        "timeout with --timeout."
    )


class TLSError(NetworkError):
    default_hint = (
        "TLS certificate verification failed. If your organisation uses an internal CA, "
        "enable 'Trust macOS Keychain CAs' or provide a CA bundle (PEM) path. Disabling "
        "verification is insecure and should only be used for diagnosis."
    )


class CancelledByUser(JiraError):
    pass


_SSO_HINT = (
    "A redirect usually means an SSO/login page (SAML, OIDC, a reverse proxy) or a wrong "
    "base URL. Use the Jira base URL and a personal access token rather than browser login."
)


def parse_error_body(response: httpx.Response) -> Tuple[List[str], Dict[str, str]]:
    """Return ``(errorMessages, errors)`` from a Jira error body (lenient)."""
    try:
        data = response.json()
    except Exception:
        return [], {}
    msgs: List[str] = []
    errs: Dict[str, str] = {}
    if isinstance(data, dict):
        em = data.get("errorMessages")
        if isinstance(em, list):
            msgs.extend(str(m) for m in em if m)
        er = data.get("errors")
        if isinstance(er, dict):
            errs.update({str(k): str(v) for k, v in er.items()})
        elif isinstance(er, list):  # some plugins / Bitbucket-style bodies
            msgs.extend(str(e.get("message")) for e in er if isinstance(e, dict) and e.get("message"))
        if not msgs and not errs and data.get("message"):
            msgs.append(str(data["message"]))
    return msgs, errs


def _is_html(response: httpx.Response) -> bool:
    return "html" in response.headers.get("content-type", "").lower()


def _html_title(response: httpx.Response) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", response.text[:5000], re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else ""


def auth_failure_from_headers(response: httpx.Response) -> Optional[JiraError]:
    """Detect Jira's header-only authentication failures (any status code).

    Jira may answer a request with *bad* credentials anonymously (HTTP 200 for
    anonymous-readable resources) while flagging ``X-Seraph-LoginReason:
    AUTHENTICATED_FAILED``. ``X-Authentication-Denied-Reason`` carries the CAPTCHA
    challenge (``CAPTCHA_CHALLENGE; login-url=...``).
    """
    denied = response.headers.get(DENIED_HEADER, "")
    reason = response.headers.get(SERAPH_HEADER, "")
    if denied:
        kind = denied.split(";")[0].strip() or denied
        return CaptchaRequiredError(
            f"Authentication denied by Jira ({kind})",
            status=response.status_code,
        )
    if "AUTHENTICATION_DENIED" in reason:
        return CaptchaRequiredError(
            "Authentication denied by Jira (X-Seraph-LoginReason: AUTHENTICATION_DENIED)",
            status=response.status_code,
        )
    if "AUTHENTICATED_FAILED" in reason:
        return AuthenticationError(
            "Credentials were not accepted (X-Seraph-LoginReason: AUTHENTICATED_FAILED)",
            status=response.status_code,
        )
    return None


def error_from_response(response: httpx.Response) -> JiraError:
    """Map a non-2xx response to a typed error."""
    status = response.status_code
    msgs, errs = parse_error_body(response)
    parts = list(msgs) + [f"{k}: {v}" for k, v in errs.items()]
    server_msg = "; ".join(parts)
    kw: Dict[str, Any] = {"status": status, "error_messages": msgs, "field_errors": errs}

    if 300 <= status < 400:
        location = response.headers.get("location", "?")
        return UnexpectedResponseError(f"Server redirected to {location}", hint=_SSO_HINT, **kw)
    header_err = auth_failure_from_headers(response)
    if header_err is not None and status in (401, 403):
        header_err.error_messages, header_err.field_errors = msgs, errs
        return header_err
    if not server_msg and _is_html(response) and status in (401, 403, 404):
        title = _html_title(response)
        server_msg = f"HTML page instead of JSON{': ' + repr(title) if title else ''}"
    if not server_msg and not _is_html(response):
        # Jira's JSON body reader answers malformed requests with text/plain
        # (e.g. "Cannot deserialize value of type `java.lang.Boolean` ...").
        server_msg = redact(response.text.strip().split("\n", 1)[0])[:300]
    if status == 401:
        return AuthenticationError(server_msg or "Authentication required / failed", **kw)
    if status == 403:
        return PermissionDeniedError(server_msg or "Forbidden", **kw)
    if status == 404:
        return NotFoundError(server_msg or "Not found", **kw)
    if status == 400:
        return BadRequestError(server_msg or "Bad request", **kw)
    if status == 409:
        return ConflictError(server_msg or "Conflict", **kw)
    if status == 429:
        return RateLimitedError(server_msg or "Too many requests", **kw)
    if status >= 500:
        return ServerError(server_msg or f"Server error {status}", **kw)
    return JiraError(server_msg or f"Unexpected HTTP {status}", **kw)


def _is_tls_failure(exc: BaseException) -> bool:
    seen = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLError):
            return True
        text = str(cur)
        if "CERTIFICATE_VERIFY_FAILED" in text or "SSL" in text and "certificate" in text.lower():
            return True
        cur = cur.__cause__ or cur.__context__
    return False


_DNS_MARKERS = ("nodename nor servname", "Name or service not known", "getaddrinfo failed",
                "Temporary failure in name resolution", "No address associated")


def error_from_transport(exc: BaseException, url: str = "") -> JiraError:
    """Map an httpx transport exception to a typed error."""
    where = f" ({url})" if url else ""
    if isinstance(exc, httpx.TimeoutException):
        kind = type(exc).__name__
        return TimeoutError_(f"Request timed out{where} [{kind}]")
    if _is_tls_failure(exc):
        return TLSError(f"TLS/SSL error{where}: {exc}")
    if isinstance(exc, httpx.ConnectError):
        text = str(exc)
        if any(m in text for m in _DNS_MARKERS):
            return NetworkError(
                f"DNS lookup failed{where}: {exc}",
                hint="The host name could not be resolved. Check the URL spelling and that the VPN is connected.",
            )
        return NetworkError(f"Could not connect{where}: {exc}")
    if isinstance(exc, httpx.TransportError):
        return NetworkError(f"Network error{where}: {exc}")
    return JiraError(f"Unexpected error{where}: {exc}")
