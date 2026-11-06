"""Tiny stdlib HTTP client. All network access in plane_watch goes through here,
so tests can mock get_json / get_bytes / post_json."""

from __future__ import annotations

import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = os.environ.get(
    "PLANE_WATCH_UA", "plane-watch/1.0 (+https://github.com/Inasjackw321/hybrid-war-tracker)"
)
TIMEOUT = 15
DEAD_FOR_S = 300  # after a connection failure, skip that host for this long

_dead_hosts: dict[str, float] = {}


class FetchError(RuntimeError):
    def __init__(self, msg: str, network: bool = False):
        super().__init__(msg)
        self.network = network  # True = couldn't connect / timed out (not an HTTP error)


def dead_hosts() -> list[str]:
    now = time.time()
    return sorted(h for h, until in _dead_hosts.items() if until > now)


def _request(url: str, data: bytes | None = None, headers: dict | None = None) -> bytes:
    if os.environ.get("PLANE_WATCH_OFFLINE"):
        raise FetchError(f"offline mode: not fetching {url}")
    host = urllib.parse.urlsplit(url).hostname or ""
    if _dead_hosts.get(host, 0) > time.time():
        raise FetchError(f"{host} skipped: it didn't respond a moment ago", network=True)
    hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip", "Accept": "application/json, */*"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip" or body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            return body
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code} from {url}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        _dead_hosts[host] = time.time() + DEAD_FOR_S
        raise FetchError(f"{host} unreachable ({getattr(e, 'reason', e)}); skipping it for {DEAD_FOR_S // 60} min", network=True) from None


def get_bytes(url: str, headers: dict | None = None) -> bytes:
    return _request(url, headers=headers)


def get_json(url: str, headers: dict | None = None):
    body = _request(url, headers=headers)
    try:
        return json.loads(body)
    except ValueError:
        raise FetchError(f"{url} did not return JSON") from None


def post_json(url: str, payload, headers: dict | None = None):
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    body = _request(url, data=json.dumps(payload).encode(), headers=hdrs)
    try:
        return json.loads(body)
    except ValueError:
        raise FetchError(f"{url} did not return JSON") from None
