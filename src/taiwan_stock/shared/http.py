"""Shared `httpx2.Client` factory for every real (non-test) network call.

Several TWSE/TPEx/投信 sources sit behind bot-detection WAFs (design
005 §4.4 follow-up: 國泰's custodian-bank page returned HTTP 403 from
Akamai's edge — `errors.edgesuite.net` — for a client with no
`User-Agent`, and 200 once given one). Every real network call in this
app builds its client here instead of each call site inventing its own
headers, so a source that starts blocking a bare-bones client doesn't
need to be independently rediscovered and patched in every caller.
"""

import httpx2

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}


# Design 068: a broken source must not fill memory. Counts transferred
# (possibly compressed) bytes.
MAX_RESPONSE_BYTES = 50 * 1024 * 1024


class ResponseTooLarge(ValueError):
    """A ValueError so sync records it as parse_failed and doesn't retry."""

    def __init__(self, limit):
        super().__init__(f"外部回應超過 {limit // (1024 * 1024)} MB 上限")


class _LimitedStream(httpx2.SyncByteStream):
    def __init__(self, stream, limit):
        self._stream, self._limit = stream, limit

    def __iter__(self):
        received = 0
        for chunk in self._stream:
            received += len(chunk)
            if received > self._limit:
                raise ResponseTooLarge(self._limit)
            yield chunk

    def close(self):
        self._stream.close()


def _limit_response(response: httpx2.Response) -> None:
    limit = MAX_RESPONSE_BYTES
    declared = response.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > limit:
        raise ResponseTooLarge(limit)
    response.stream = _LimitedStream(response.stream, limit)


def new_client(*, timeout: float = 30.0, **kwargs) -> httpx2.Client:
    hooks = dict(kwargs.pop("event_hooks", None) or {})
    hooks["response"] = [_limit_response, *hooks.get("response", [])]
    hooks.setdefault("request", [])
    return httpx2.Client(timeout=timeout, headers=_HEADERS, event_hooks=hooks, **kwargs)
