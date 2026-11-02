"""Origin-header CSRF defense (design 002 §4.14).

This app has no login or session, so the classic token-based CSRF
defense (compare a form-embedded token against a session) doesn't
apply — there is no session to steal. The actual threat here is a
malicious page open in the user's browser silently submitting a
state-changing request to this locally-bound server on the user's
behalf. A browser cannot be scripted to lie about the `Origin` header
on a cross-origin POST/PUT/PATCH/DELETE (unlike a body field, which a
crafted cross-origin form can set to anything), so rejecting any such
request whose `Origin` doesn't match this server's own origin blocks
exactly that attack without requiring a token in every form and JSON
payload. A request with no `Origin` header (same-origin browser
requests in some cases, `TestClient`, curl, CLI tooling) is allowed
through — it carries no attacker-controlled cross-origin claim to
reject in the first place.

Issue #87: "this server's own origin" must not be derived from the
`Host` header alone. A DNS-rebinding page makes its own domain resolve
to 127.0.0.1, so its `Origin` and `Host` agree on the attacker's name.
`TrustedHostMiddleware` therefore rejects any `Host` outside the local
names (plus an explicit `--host`) before a page or write is served.
"""

from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# Binding every interface says nothing about which name a browser may use.
_WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::"})


def trusted_hosts(bind_host: str | None = None) -> frozenset[str]:
    """The local names, plus `--host` when it names one specific address."""
    host = (bind_host or "").strip("[]").lower()
    return LOCAL_HOSTS if host in _WILDCARD_HOSTS else LOCAL_HOSTS | {host}


def _hostname(netloc: str) -> str | None:
    # Unlike a `split(":")`, this keeps IPv6 literals such as "[::1]:2330".
    try:
        return urlsplit(f"//{netloc}").hostname
    except ValueError:
        return None


class TrustedHostMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, hosts: frozenset[str]) -> None:
        super().__init__(app)
        self.hosts = hosts

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if _hostname(request.headers.get("host", "")) not in self.hosts:
            return PlainTextResponse("Invalid host header", status_code=400)
        return await call_next(request)


class OriginCheckMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, hosts: frozenset[str]) -> None:
        super().__init__(app)
        self.hosts = hosts

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.method in UNSAFE_METHODS:
            # Defense in depth (#115): the browser's own cross-site verdict.
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse(
                    {"detail": "Cross-origin request rejected"}, status_code=403
                )
            origin = request.headers.get("origin")
            if origin is not None and (
                origin != _expected_origin(request)
                or _hostname(urlsplit(origin).netloc) not in self.hosts
            ):
                return JSONResponse(
                    {"detail": "Cross-origin request rejected"}, status_code=403
                )
        return await call_next(request)


def _expected_origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"
