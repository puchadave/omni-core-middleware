# Omni-Core Architecture Review — 2026-11-15

## Current assessment

Omni-Core is a functional FastAPI ERP/BI middleware with PostgreSQL/SQLite support, Matterhorn import, Shopware integration, Google Merchant operations, OmniRoute provider/model/routing management, competitor intelligence, event bus and a Global Control Center.

The repository is operationally feature-rich but structurally ahead of its hardening layer: the central application file is monolithic, a legacy `omni_core/` implementation coexists with the current root application, and some modules still expose placeholder business logic.

## Finalized in this revision

- Unified root dashboard as Global Control Center.
- Added browser login using the existing `INTERNAL_API_KEY`.
- Added short-lived HttpOnly signed browser sessions.
- Preserved direct API authentication through `X-Internal-API-Key`.
- Added automatic `INTERNAL_API_KEY` generation to `start.sh`.
- Added runtime uptime/database telemetry to the control-center payload.
- Added operational actions for Matterhorn import and AI trend analysis.
- Added responsive system, AI, marketplace, BI, security and activity views.
- Added browser-session regression tests.
- Corrected the Settings navigation route.
- Documented the control-plane UI.

## Remaining architectural work

### P0 — Production hardening
1. Split `main.py` into FastAPI routers and service composition.
2. Replace the in-process fulfillment queue with a durable queue or database-backed job scheduler.
3. Persist EventBus events in a dedicated event table rather than relying primarily on system logs.
4. Implement real authorization roles instead of one shared internal API secret.
5. Move integration secrets to a proper secret store for production deployments.
6. Encrypt OAuth/API credentials at rest with a dedicated key-management mechanism.

### P1 — Reliability
1. Add idempotency keys to all external write operations.
2. Add retry/circuit-breaker telemetry to the dashboard.
3. Add dead-letter inspection and replay controls to the UI.
4. Add database migration/version management instead of startup-only schema initialization.
5. Add scheduled health checks and integration SLA/error-rate metrics.

### P1 — Commerce intelligence
1. Complete real sales/marketing database integration where placeholder endpoints remain.
2. Add channel-level margin, contribution margin and price-floor calculations.
3. Add competitor price snapshots and historical price curves.
4. Add bundle profitability and stock-constrained bundle scoring.
5. Add automated export diffing before marketplace publication.

### P2 — Maintainability
1. Consolidate or explicitly archive the legacy `omni_core/` implementation.
2. Establish a typed domain model for canonical products, orders, channels and AI jobs.
3. Add contract tests for Matterhorn, Shopware, Google Merchant and OmniRoute.
4. Add CI with lint, type checks, unit tests and smoke tests.
5. Version the public API.

## Release position

The system is now materially more usable as an operator-facing control plane. It should be treated as a hardened internal middleware platform, not yet as a fully regulated multi-tenant SaaS control plane.
