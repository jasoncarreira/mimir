"""HTTP seam for tracker adapters.

Adapters never touch the network directly: they call a ``TrackerTransport``, so
tests replay recorded exchanges and production uses ``UrllibTransport``. Failures
surface as ``TrackerUnavailable`` carrying only a fixed code, never response text,
because tracker responses can hold attacker-written prose.
"""

from __future__ import annotations

import http.client
import json as _json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

_CODE_RE = re.compile(r"transport|timeout|rate_limited|malformed_payload|http_[1-5][0-9]{2}")
_MAX_RESPONSE_BYTES = 8_000_000


class TrackerUnavailable(Exception):
    """The tracker could not give a usable answer; ``code`` is a fixed token."""

    def __init__(self, code: str) -> None:
        if _CODE_RE.fullmatch(code) is None:
            raise ValueError(f"invalid tracker unavailable code: {code!r}")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class HttpResponse:
    status: int
    json: Any = None


def unavailable_for_status(status: int) -> TrackerUnavailable:
    """Return the ``TrackerUnavailable`` an adapter raises for an unexpected status."""

    if status == 429:
        return TrackerUnavailable("rate_limited")
    return TrackerUnavailable(f"http_{status}" if 100 <= status <= 599 else "transport")


class TrackerTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Any = None,
        timeout: float,
    ) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class UrllibTransport:
    """``TrackerTransport`` over urllib: https only, redirects are not followed.

    Any HTTP status is returned as a response; only network failure raises.
    """

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Any = None,
        timeout: float,
    ) -> HttpResponse:
        if not url.startswith("https://"):
            raise TrackerUnavailable("transport")
        data = None
        request_headers = dict(headers)
        if json is not None:
            data = _json.dumps(json).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=timeout) as response:  # noqa: S310 - https checked above
                body = _parse(response.read(_MAX_RESPONSE_BYTES))
                if body is _UNPARSED:
                    raise TrackerUnavailable("malformed_payload")
                return HttpResponse(response.status, body)
        except urllib.error.HTTPError as exc:
            try:
                body = _parse(exc.read(_MAX_RESPONSE_BYTES))
            except (http.client.HTTPException, OSError):
                raise TrackerUnavailable("transport") from None
            finally:
                exc.close()
            return HttpResponse(exc.code, None if body is _UNPARSED else body)
        except TimeoutError:
            raise TrackerUnavailable("timeout") from None
        except urllib.error.URLError as exc:
            raise TrackerUnavailable(
                "timeout" if isinstance(exc.reason, TimeoutError) else "transport"
            ) from None
        except (OSError, ValueError, http.client.HTTPException):
            raise TrackerUnavailable("transport") from None


_UNPARSED = object()


def _parse(raw: bytes) -> Any:
    if not raw.strip():
        return None
    try:
        return _json.loads(raw)
    except ValueError:
        return _UNPARSED


def default_transport() -> TrackerTransport:
    """Return the production transport; tests monkeypatch this."""

    return UrllibTransport()
