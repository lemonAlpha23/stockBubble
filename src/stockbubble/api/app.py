"""FastAPI application factory.

The factory exists so tests can build an app around fake services without any
network, and so the CLI owns the only place that knows about real adapters.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from stockbubble import __version__
from stockbubble.api.routes import UiSettings, build_router
from stockbubble.application.services import AppServices
from stockbubble.domain.errors import BubbleError

logger = logging.getLogger(__name__)


def create_app(
    *,
    services: AppServices,
    ui: UiSettings | None = None,
    web_dir: Path | None = None,
) -> FastAPI:
    settings = ui or UiSettings()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info("stockbubble %s ready", __version__)
        try:
            yield
        finally:
            await services.aclose()
            logger.info("stockbubble stopped")

    app = FastAPI(
        title=settings.title,
        version=__version__,
        description="Bubble chart market dashboard: crypto + US equities.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.services = services
    app.state.ui = settings
    app.include_router(build_router(services, settings))

    @app.exception_handler(BubbleError)
    async def _bubble_error(request: Request, exc: BubbleError) -> JSONResponse:
        if exc.status >= 500:
            logger.warning("%s %s -> %s", request.method, request.url.path, exc.code, exc_info=exc)
        return JSONResponse(status_code=exc.status, content={"error": exc.as_payload()})

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = ", ".join(
            ".".join(str(part) for part in err.get("loc", ())[1:]) or "query"
            for err in exc.errors()
        )
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": f"请求参数不合法：{fields}",
                    "hint": "检查窗口/口径/limit 的取值范围",
                }
            },
        )

    @app.middleware("http")
    async def _cache_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        elif path == "/" or path.endswith((".html", ".css", ".js")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    if web_dir is not None and web_dir.is_dir():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    else:
        logger.warning("web directory %s not found; serving API only", web_dir)

        @app.get("/", include_in_schema=False)
        async def _no_ui() -> JSONResponse:
            return JSONResponse(
                status_code=200,
                content={
                    "service": settings.title,
                    "version": __version__,
                    "note": "frontend assets missing; API lives under /api",
                    "docs": "/api/docs",
                },
            )

    return app
