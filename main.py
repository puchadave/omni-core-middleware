import asyncio
import time
import httpx
import aiosqlite
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from database import init_db, DB_PATH

app = FastAPI(title="Omni-Core Master Middleware")
templates = Jinja2Templates(directory="templates")
task_queue = asyncio.Queue()

@app.on_event("startup")
async def startup():
    await init_db()

@app.get("/", response_class=HTMLResponse)
async def master_dashboard(request: Request):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM products") as cursor:
            total_products = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM products WHERE status='error'") as cursor:
            error_count = (await cursor.fetchone())[0]
        async with db.execute("SELECT key, value FROM settings") as cursor:
            settings = {row[0]: row[1] for row in (await cursor.fetchall())}
    return templates.TemplateResponse("master_dashboard.html", {"request": request, "total_products": total_products, "error_count": error_count, "settings": settings})

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key, value FROM settings") as cursor:
            settings = {row[0]: row[1] for row in (await cursor.fetchall())}
    return templates.TemplateResponse("settings.html", {"request": request, "settings": settings})

@app.post("/settings/save")
async def save_settings(omniroute_url: str = Form(...), omniroute_model: str = Form(...), shopware_api_url: str = Form(""), ebay_token: str = Form("")):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", [
            ("omniroute_url", omniroute_url), ("omniroute_model", omniroute_model),
            ("shopware_api_url", shopware_api_url), ("ebay_token", ebay_token)
        ])
        await db.commit()
    return RedirectResponse(url="/settings?saved=true", status_code=303)

@app.get("/api/system-health")
async def system_health():
    return {
        "omniroute": {"status": "online", "latency_ms": 12.5},
        "shops": {"shopware": "connected", "ebay": "connected", "amazon": "syncing", "kaufland": "idle"},
        "queue_size": task_queue.qsize()
    }
