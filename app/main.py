from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.auth import browser_login_redirect, should_redirect_browser_to_login
from app.routes_auth import router as auth_router
from app.routes_ops import router as ops_router
from app.routes_requester import router as requester_router
from app.ui import STATIC_DIR
from shared.config import get_settings
from shared.db import ping_database
from shared.logging import log_web_event
from shared.workspace import verify_workspace_contract_paths


def create_app() -> FastAPI:
    app = FastAPI(title="Stage 1 AI Triage MVP")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(auth_router)
    app.include_router(requester_router)
    app.include_router(ops_router)

    @app.middleware("http")
    async def structured_request_logging(request: Request, call_next):
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            log_web_event(
                "request_failed",
                level="error",
                method=request.method,
                path=request.url.path,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                error=str(exc),
            )
            raise

        log_web_event(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return response

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        try:
            settings = get_settings()
            settings.validate_contracts()
            ping_database(settings)
            verify_workspace_contract_paths(settings)
        except Exception as exc:
            log_web_event("readiness_failed", level="error", error=str(exc))
            return JSONResponse({"status": "not_ready", "error": str(exc)}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/", include_in_schema=False)
    def root_redirect() -> RedirectResponse:
        return RedirectResponse("/app", status_code=status.HTTP_302_FOUND)

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException):
        if should_redirect_browser_to_login(request, exc.status_code):
            return browser_login_redirect(request)
        return await http_exception_handler(request, exc)

    return app


app = create_app()
