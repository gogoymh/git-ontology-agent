"""Tiny JSON-over-HTTP client on the standard library. Tests swap `transport`."""
from __future__ import annotations

import email.utils
import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Mapping, Optional

from . import log

_log = log.get("http")

MAX_WAIT = 60.0  # longest pause for a rate limit (OpenRouter free tiers reset per minute)


class HTTPError(RuntimeError):
    """`headers` are the response headers (lowercase names); they never hold our API key."""

    def __init__(self, status: int, body: str, headers: Optional[Mapping[str, str]] = None):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


def _urllib(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise HTTPError(e.code, e.read().decode(errors="replace"), dict(e.headers.items())) from None


transport: Callable[[str, Dict[str, str], Dict[str, Any], float], Dict[str, Any]] = _urllib


def _seconds_until(value: Any, now: float) -> Optional[float]:
    """A reset hint as seconds from now: delta seconds, epoch seconds, epoch ms, or an HTTP date."""
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        try:
            return email.utils.parsedate_to_datetime(str(value)).timestamp() - now
        except (TypeError, ValueError):
            return None
    if v > 1e12:
        return v / 1000 - now
    if v > 1e9:
        return v - now
    return v


def retry_after(e: HTTPError, now: Optional[float] = None) -> Optional[float]:
    """How long a 429 asks us to wait: Retry-After, X-RateLimit-Reset, or the error body's metadata
    (OpenRouter puts upstream headers in `error.metadata.headers`). Capped at MAX_WAIT."""
    now = time.time() if now is None else now
    hints = [e.headers.get("retry-after"), e.headers.get("x-ratelimit-reset")]
    try:
        meta = (json.loads(e.body or "{}").get("error") or {}).get("metadata") or {}
    except (ValueError, AttributeError):
        meta = {}
    if isinstance(meta, dict):
        mh = {str(k).lower(): v for k, v in (meta.get("headers") or {}).items()} if isinstance(meta.get("headers"), dict) else {}
        hints += [meta.get("retry_after"), mh.get("retry-after"), mh.get("x-ratelimit-reset")]
    for h in hints:
        s = _seconds_until(h, now)
        if s is not None:
            return max(1.0, min(MAX_WAIT, s))  # a reset already passed still gets a beat
    return None


def post_json(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: float = 60.0,
              retries: int = 2) -> Dict[str, Any]:
    """POST JSON with retry on 429/5xx. A 429 waits until the advertised reset (at most MAX_WAIT).
    Never logs request headers (they hold keys)."""
    where = url.split("?")[0]
    for attempt in range(retries + 1):
        started = time.monotonic()
        try:
            out = transport(url, headers, body, timeout)
            _log.debug("POST %s ok in %.0f ms", where, (time.monotonic() - started) * 1000)
            return out
        except HTTPError as e:
            retriable = e.status == 429 or e.status >= 500
            _log.warning("POST %s failed (%s), attempt %d", where, e.status, attempt + 1)
            if not retriable or attempt == retries:
                raise
            hint = retry_after(e) if e.status == 429 else None
            wait = 0.5 * 2 ** attempt if hint is None else hint
            if e.status == 429:
                _log.warning("POST %s rate limited, waiting %.1f s (%s)", where, wait,
                             "until the advertised reset" if hint is not None else "no reset hint, backoff")
            time.sleep(wait)
    raise AssertionError("unreachable")
