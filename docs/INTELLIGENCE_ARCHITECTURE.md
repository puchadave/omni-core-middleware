# Intelligence Architecture – Omni-Core

## Purpose

This document records the Phase Zero audit of the existing Omni-Core architecture and the proposed integration points for the new intelligence, creative, and social-commerce expansion.

## Existing Architecture

- FastAPI application in `main.py`, version `2.1.0`.
- Server-rendered Jinja2 pages and JSON APIs.
- Unified persistence in `database.py` with PostgreSQL through asyncpg and SQLite WAL fallback.
- Automatic schema creation through `init_db()`; no separate migration tool is currently used.
- In-process `asyncio.Queue` with one fulfillment worker; durable state is database-backed.
- Event bus and `GlobalControlCenter` are integrated into runtime state.
- Security boundary is the internal API key middleware plus an initial signed admin session implementation.
- Existing admin pages: `/`, `/admin`, `/admin/products`, `/admin/orders`, `/admin/settings`, `/google`, `/google/export`, `/omniroute`, `/sales_marketing_tower`, `/photoshoot`.

## Existing Data Domains

- Core commerce: `products`, `bundles`, `orders`, `channel_content`, `import_runs`, `system_logs`.
- Google Merchant Center: accounts, credentials, data sources, settings, mappings, exports, sync jobs, sync errors, diagnostics, performance metrics, audit logs.
- OmniRoute/AI: `ai_providers`, `ai_provider_credentials`, `ai_models`, `ai_model_capabilities`, `ai_routing_rules`, `ai_routing_policies`, `ai_workflows`, `ai_workflow_configs`, `ai_usage_records`, `ai_cost_records`, `ai_budgets`, `ai_provider_health`, `ai_request_logs`, `ai_error_logs`, `ai_audit_logs`.
- Commerce intelligence: `competitors`, `competitor_products`, `product_matches`, `competitor_price_history`, `monitoring_jobs`, `price_alerts`, `pricing_recommendations`, `pricing_rules`, `research_runs`.
- Settings: `settings` and `google_settings`.

## Existing Capability Map

- Product import, product list/detail, bundle stock recalculation, order persistence, stock reservation, pricing calculation, trend analysis, logs, imports, exports, Shopware, Google Merchant Center, competitor monitoring, OmniRoute provider/model/routing/cost/budget management.
- `services/model_reference.py` currently defines a fixed in-memory model list and local/GitHub/OmniRoute path conventions.
- `services/image_manager.py` performs local filesystem image downloads, SHA-256 deduplication, metadata extraction, and thumbnail-oriented storage.
- `services/ai_photoshooting.py` defines `ImageGenerationProvider`, `OmniRouteProvider`, and `LocalPlaceholderProvider`; `/api/photoshoot/start` now calls `get_provider().generate()`.
- `services/omniroute_client.py` exposes health, AI trend analysis, and SEO optimization.
- `services/omniroute_provider.py` and `services/omniroute_model_registry.py` provide provider registration, model lookup, routing, circuit-breaker, and cost calculation.

## Gaps for the New Platform

- No durable asset, asset-version, model-profile, model-reference, or model-permission tables.
- No creative project, creative job, creative output, video project, video scene, campaign, or approval tables.
- No agent profile, conversation, run, tool-call, or approval persistence.
- No product-quality, channel-readiness, supplier intelligence, demand-forecast, or returns-intelligence persistence.
- No social account, platform connection, content, editorial calendar, publishing job, publication, analytics, hashtag, trend, or brand-profile persistence.
- No central agent tool registry or persistent creative workflow state.
- No image/video generation abstraction through the central OmniRoute client; photoshooting uses its own provider implementation.
- No durable job queue or dead-letter/retry persistence; the queue is in-memory.
- Several existing endpoints are structural or placeholder implementations and must not be treated as complete functionality.

## Security and Compliance Baseline

- Internal API key middleware is fail-closed for all non-excluded routes.
- Excluded routes: `/api/health`, `/docs`, `/redoc`, `/openapi.json`.
- Secret-bearing settings are masked through `is_secret_key`.
- Admin session cookies are signed with `SESSION_SECRET`; `require_admin` validates them.
- No complete user/RBAC model, CSRF protection, rate limiting, or full audit trail is currently implemented.
- Real-person image/model use requires documented consent and must be blocked without valid rights.

## Migration Strategy

- Use the existing dual-backend schema pattern: add equivalent statements to `POSTGRES_SCHEMA` and `SQLITE_SCHEMA`.
- Prefer additive, nullable, or default-backed columns and new tables; do not alter or remove existing product master data.
- Use stable text IDs and foreign keys with `ON DELETE CASCADE` where appropriate.
- Keep local filesystem storage as the first backend and add an object-storage abstraction later.
- Do not introduce a second database migration framework in the first implementation pass.
