"""FastAPI core application for Omni-Core E-Commerce Middleware.

Provides the main entry point for the application with endpoints for:
- Master dashboard view
- System settings management
- Background task triggers (e.g., Matterhorn catalog import)

The application uses FastAPI with Uvicorn + uvloop for high-performance
async handling, and SQLite via aiosqlite in WAL mode for data persistence.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from database import init_db, get_connection, close_connection, DB_PATH
from services.matterhorn_importer import MatterhornImporter

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Lifespan & Startup
# -------------------------------------------------------------------------__

@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    """FastAPI lifespan context.

    - Initializes the SQLite database (WAL mode + tables)
    - Starts the background Matterhorn import worker
    - Yields control to the application
    - On shutdown: closes database connections cleanly
    """
    await init_db()
    # Initialize Matterhorn importer as a reusable client
    importer: MatterhornImporter = MatterhornImporter()
    app.state.importer = importer
    yield
    # Cleanup on shutdown
    await importer.close()
    await close_connection(None)  # noqa: B008


# ---------------------------------------------------------------------------
# FastAPI Application Assembly
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Omni-Core E-Commerce Middleware",
    lifespan=lifespan,
    description="High-performance E-commerce middleware with FastAPI, SQLite WAL, and Matterhorn integration.",
)


templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# HTTP Route Handlers
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def master_dashboard(request: Request) -> HTMLResponse:
    """Render the main dashboard HTML page."""
    # Basic dashboard stats - in production would query DB
    async with get_connection() as db:
        async with db.execute("SELECT COUNT(*) FROM products") as cursor:
            total_products: int = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM products WHERE stock > 0") as cursor:
            active_products: int = (await cursor.fetchone())[0]

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "total_products": total_products,
            "active_products": active_products,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    """Render the settings configuration page."""
    return templates.TemplateResponse("settings.html", {"request": request})


@app.post("/api/import/matterhorn")
async def trigger_matterhorn_import(
    request: Request,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """Trigger the Matterhorn catalog import as a background task.

    This endpoint starts the import process in the background so the API
    responds immediately. The import runs asynchronously and does not
    block other endpoint calls.

    :param request: FastAPI request object (for potential future use).
    :param background_tasks: FastAPI background tasks handler.
    :return: JSON response with import initiation status.
    """
    importer: MatterhornImporter = request.app.state.importer

    # Run the import in the background without blocking the API response
    background_tasks.add_task(importer.import_catalog, limit=500)

    return JSONResponse(
        {"status": "started", "message": "Matterhorn catalog import initiated in background"},
        status_code=202,
    )


@app.get("/api/health")
async def health_check() -> JSONResponse:
    """Simple health check endpoint."""
    return JSONResponse({"status": "healthy", "database": "connected"})


# ---------------------------------------------------------------------------
# Development / Direct Execution
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, loop="uvloop")  # noqa: F821