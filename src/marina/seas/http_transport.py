"""Shared HTTP seam for REST-backed seas (Verda, Clore, ...).

VastSea shells out to a CLI; the REST seas instead need a tiny, injectable HTTP
client so unit/service tests can script responses without a network. The
default transport uses the Python stdlib (`urllib`) — no third-party dependency.
Per-call headers (auth token, a browser User-Agent for Cloudflare-fronted APIs)
are supplied by the caller, so this module stays provider-agnostic.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

DEFAULT_TIMEOUT_S = 45.0


@dataclass(frozen=True)
class HttpResponse:
    """A minimal HTTP response (decoupled from `requests`, for testability)."""

    status_code: int
    text: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> object:
        return json.loads(self.text) if self.text else None


class Transport(Protocol):
    """How a REST sea performs an HTTP call; the seam tests inject a fake at."""

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        json_body: object = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> HttpResponse: ...


class TransportError(Exception):
    """Raised when the transport cannot reach the host at all (not an HTTP 4xx/5xx)."""


def urllib_transport(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json_body: object = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> HttpResponse:
    """Default transport: stdlib urllib (no third-party dependency).

    An HTTP error status (4xx/5xx) is returned as an HttpResponse (so callers
    read the body for the API's error code); a true connection failure raises
    TransportError.
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    hdrs = dict(headers or {})
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return HttpResponse(resp.status, resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return HttpResponse(exc.code, body)
    except urllib.error.URLError as exc:
        raise TransportError(str(exc.reason)) from exc
