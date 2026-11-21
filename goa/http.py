"""Tiny JSON-over-HTTP client on the standard library. Tests swap `transport`."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict

from . import log

_log = log.get("http")


class HTTPError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def _urllib(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise HTTPError(e.code, e.read().decode(errors="replace")) from None


transport: Callable[[str, Dict[str, str], Dict[str, Any], float], Dict[str, Any]] = _urllib


def post_json(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: float = 60.0,
              retries: int = 2) -> Dict[str, Any]:
    """POST JSON with retry on 429/5xx. Never logs headers (they hold keys)."""
    for attempt in range(retries + 1):
        started = time.monotonic()
        try:
            out = transport(url, headers, body, timeout)
            _log.debug("POST %s ok in %.0f ms", url.split("?")[0], (time.monotonic() - started) * 1000)
            return out
        except HTTPError as e:
            retriable = e.status == 429 or e.status >= 500
            _log.warning("POST %s failed (%s), attempt %d", url.split("?")[0], e.status, attempt + 1)
            if not retriable or attempt == retries:
                raise
            time.sleep(0.5 * 2 ** attempt)
    raise AssertionError("unreachable")
