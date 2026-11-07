"""FastAPI entry point for the Taiwan Stock web application.

`create_app` is a factory rather than a bare module-level `app` so
tests can build a fully isolated instance pointed at a temp data
directory (`create_app(tmp_path)`) instead of sharing — or worse,
silently mutating — whatever real database the D07 default resolves
to on the machine running the tests.
"""

import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from taiwan_stock.infrastructure import migrate, sync_jobs
from taiwan_stock.infrastructure.db import (
    connection_dependency,
    get_connection,
    get_runtime_connection,
    resolve_data_dir,
)
from taiwan_stock.shared.version import get_version
from taiwan_stock.web import (
    pages_corporate_actions,
    pages_corporate_reviews,
    pages_holdings,
    pages_income,
    pages_securities,
    pages_security_detail,
    pages_settings,
    pages_transactions,
    routes_backup,
    routes_catalog,
    routes_etf_scale,
    routes_fee_quotes,
    routes_management,
    routes_market_calendar,
    routes_portfolio,
    routes_sync_jobs,
)
from taiwan_stock.web.csrf import (
    OriginCheckMiddleware,
    TrustedHostMiddleware,
    trusted_hosts,
)
from taiwan_stock.web.deps import get_data_dir, get_db

SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
}


def create_app(
    data_dir: Path,
    *,
    bind_host: str | None = None,
    extra_trusted_hosts: tuple[str, ...] = (),
) -> FastAPI:
    data_dir = resolve_data_dir(data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup must not raise here: doing so would stop uvicorn from
        # binding the port at all, taking /health down along with every
        # other route. A migration failure (locked/unwritable data
        # directory, edited-migration checksum mismatch) is instead
        # recorded on app.state so /health can report it — the data
        # directory being broken is real information a liveness check
        # should surface, not something that should make the process
        # itself unreachable.
        app.state.startup_error = None
        try:
            conn = get_connection(data_dir)
            try:
                migrate.apply_pending(conn)
                migrate.validate_runtime_schema(conn)
                sync_jobs.recover(conn, data_dir)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - see comment above
            app.state.startup_error = str(exc)
        yield

    app = FastAPI(title="Taiwan Stock", lifespan=lifespan)
    app.state.data_dir = data_dir
    app.state.startup_error = None

    def readiness_error():
        if app.state.startup_error:
            return f"資料庫初始化失敗：{app.state.startup_error}"
        try:
            with closing(get_runtime_connection(data_dir)):
                pass
        except (RuntimeError, OSError, sqlite3.DatabaseError) as exc:
            return str(exc)
        return None

    @app.exception_handler(migrate.DatabaseNotReadyError)
    async def incompatible_database(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.middleware("http")
    async def require_ready_database(request, call_next):
        if request.url.path != "/health" and not request.url.path.startswith(
            "/static/"
        ):
            error = await run_in_threadpool(readiness_error)
            if error:
                # No template context: even its navigation opens the database.
                return JSONResponse({"detail": error}, status_code=503)
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request, call_next):
        # Design 065: a framing site could click our same-origin forms.
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    # Added last = outermost: an untrusted Host never reaches any route.
    hosts = trusted_hosts(bind_host, extra_trusted_hosts)
    app.add_middleware(OriginCheckMiddleware, hosts=hosts)
    app.add_middleware(TrustedHostMiddleware, hosts=hosts)
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.include_router(routes_management.router)
    app.include_router(routes_market_calendar.router)
    app.include_router(routes_etf_scale.router)
    app.dependency_overrides[get_db] = connection_dependency(data_dir)
    app.dependency_overrides[get_data_dir] = lambda: data_dir

    # Design 070: the order the routes had in the former web/pages.py.
    for module in (
        pages_holdings,
        pages_transactions,
        pages_corporate_actions,
        pages_income,
        pages_securities,
        pages_security_detail,
        pages_settings,
    ):
        app.include_router(module.router)
    app.include_router(pages_corporate_reviews.router)
    app.include_router(routes_catalog.router)
    app.include_router(routes_portfolio.router)
    app.include_router(routes_fee_quotes.router)
    app.include_router(routes_backup.router)
    app.include_router(routes_sync_jobs.router)

    @app.get("/health")
    def health() -> JSONResponse:
        startup_error = readiness_error()
        if startup_error:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "version": get_version(),
                    "detail": startup_error,
                },
            )
        return JSONResponse({"status": "ok", "version": get_version()})

    return app


app = create_app(resolve_data_dir())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=2330)
