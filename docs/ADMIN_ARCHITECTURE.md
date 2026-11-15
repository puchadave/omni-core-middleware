# Admin Architecture – Omni-Core Middleware

## Purpose

This document records the current administrative architecture of the Omni-Core middleware and serves as the technical foundation for the full administrative WebUI control center.

## Scope

- Server-rendered administrative pages.
- JSON API endpoints used by the administrative frontend.
- Backend service modules and database-backed state.
- Internal security boundary.
- Runtime and deployment context.

## Current Architecture

- The backend is a FastAPI application (`main.py`) version `2.1.0`.
- The application is intentionally single-process.
- Background jobs use an in-memory `asyncio.Queue`; durable state is stored in PostgreSQL or the SQLite WAL fallback.
- Jinja2 templates are used for server-rendered pages.
- Business logic is implemented in modular service classes under `services/`.
- Durable entities and settings are persisted through `database.py`.
- Event bus and global control center services are integrated into the runtime.

## Module Map

### Web layer

- `/` – master dashboard
- `/admin/products` – product administration
- `/admin/orders` – order administration
- `/admin/settings` – settings administration
- `/google` – Google Merchant Center dashboard
- `/google/export` – Google Merchant Center export
- `/omniroute` – OmniRoute control center

Additional existing UI surfaces:

- `/sales_marketing_tower` – sales, marketing, export, pricing, AI content
- `/photoshoot` – AI photoshoot
- `/products/{sku}` – product detail

### Backend modules

- Core app, runtime, health, stats, settings, products, orders, imports, pricing, AI trend, logs, exports.
- Sales, marketing, export channels.
- Shopware, competitor, Google Merchant Center, OmniRoute.
- Pipeline, event bus, global control center.

### Data and state

- PostgreSQL or SQLite WAL fallback.
- Durable tables for products, bundles, orders, logs, imports, Google, AI, Shopware, competitor, pricing, monitoring.
- In-memory runtime state for queues, connections, engines, routing, and control center.

## Admin Security Architecture

- Internal API key middleware is applied globally.
- Excluded paths: `/api/health`, `/docs`, `/redoc`, `/openapi.json`.
- Missing or invalid key returns `401`.
- Secret-bearing settings are masked through `is_secret_key`.
- No user authentication or role-based authorization is implemented yet; the internal key is the current administrative boundary.

## Runtime and Deployment Context

- FastAPI + Uvicorn.
- Docker and deployment scripts.
- Database-backed persistence with SQLite WAL fallback.
- Background workers for fulfillment and trend analysis.
- Integration clients managed in runtime state.

## Audit Relevance

The administrative WebUI must reuse this architecture rather than introduce a parallel backend. The existing API-first service layer and server-rendered Jinja2 pages provide the foundation for the control center.
