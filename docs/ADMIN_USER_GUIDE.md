# Admin User Guide

## Purpose

This guide describes how to operate the administrative WebUI of the Omni-Core middleware. It is based on the routes and endpoints actually present in `main.py`; endpoints that are placeholders or not fully implemented are explicitly marked.

## Access

The administrative UI is part of the FastAPI service:

- Main dashboard: `/`
- Central admin control center: `/admin`
- Products: `/admin/products`
- Orders: `/admin/orders`
- Settings: `/admin/settings`
- OmniRoute control center: `/omniroute`
- Google Merchant Center: `/google` and `/google/export`
- Sales & Marketing: `/sales_marketing_tower`
- AI photoshoot: `/photoshoot`
- API documentation: `/docs`
- OpenAPI: `/openapi.json`

The API routes are protected by the internal API key middleware. The header is:

```text
X-Internal-API-Key
```

Excluded routes are `/api/health`, `/docs`, `/redoc` and `/openapi.json`.

For the browser session there is an admin session login endpoint:

```http
POST /api/admin/session/login
Content-Type: application/x-www-form-urlencoded

api_key=<configured INTERNAL_API_KEY>
```

The endpoint sets an HttpOnly, SameSite=Lax cookie named `admin_session`. The cookie is signed with `SESSION_SECRET` and expires after one hour. If `SESSION_SECRET` is not configured, the application uses the development fallback `omni-core-dev-secret`; this fallback must not be used in production.

## Central Dashboard

The central admin page uses these data sources:

- `/api/control-center/overview` for the executive business overview.
- `/api/control-center/system` for system/module status.
- The page provides quick links to the product, order, settings, OmniRoute, Google Merchant and sales/marketing areas.

The existing main dashboard provides additional counters, recent imports, logs, queue size, database backend and uptime.

## Products

The product UI supports browsing, searching and product detail views. The following backend routes are relevant:

- `GET /api/products`
- `POST /api/products`
- `GET /api/products/{sku}`
- `GET /api/bundles`
- `GET /api/pricing/calculate` through `POST /api/pricing/calculate`
- `GET /api/import-runs`
- `POST /api/import/matterhorn`

Product creation and import actions must be confirmed before execution in production. Sensitive supplier, API and integration data must not be written to logs.

## Orders

The order UI supports the existing order list and order creation path. A created order is persisted and queued for fulfillment. If the fulfillment integration is not configured, the order remains in the reserved state instead of inventing an external result.

## Settings and Integrations

The settings page exposes the existing database-backed settings. Sensitive values are masked where the backend classifies the key as a secret. Configure at least:

- `OMNIROUTE_URL`
- `OMNIROUTE_API_KEY`
- `MATTERHORN_API_TOKEN`
- `INTERNAL_API_KEY`
- `SESSION_SECRET`

Do not commit `.env`, credentials, tokens or exported integration secrets.

Integration areas with existing implementation hooks include:

- Shopware connections, mappings, exports, descriptions and SEO.
- Google Merchant Center accounts, export queue, processing, retry, sync and health.
- OmniRoute providers, models, routing rules, routing policies, selection, cost and statistics.
- Competitor search, matching, price calculation and providers.
- Matterhorn import and product pipelines.

The UI must show unavailable integrations as not configured instead of pretending that an external operation succeeded.

## AI and Photoshoot

The photoshoot start endpoint calls the existing provider abstraction in `services/ai_photoshooting.py`:

- `POST /api/photoshoot/start`
- `GET /api/photoshoot/status/{shootId}`
- `GET /api/photoshoot/results/{shootId}`

Status and result endpoints currently return structural responses. The start endpoint is connected to `get_provider()`. Image generation requires a configured OmniRoute-compatible image provider. Generated images must not overwrite original Matterhorn images.

## Monitoring

The existing backend exposes:

- `/api/logs`
- `/api/import-runs`
- `/api/health`
- `/api/health/integrations`
- `/api/stats`
- `/api/control-center/overview`
- `/api/control-center/dashboard`
- `/api/control-center/system`

Use these endpoints for live status, error and module information. The UI should distinguish between live data, cached/in-memory state and not-configured integrations.

## Security Rules

- Never place credentials in URLs, logs, templates or Git.
- Use `SESSION_SECRET` in production and rotate it if compromised.
- Restrict access to the internal API key and the admin UI.
- Validate all form input server-side and show safe, non-sensitive error messages.
- Require confirmation for destructive actions such as deletes, exports, imports, syncs and provider changes.
- Treat the current session token as a single-admin authentication mechanism; it is not a complete multi-user RBAC system.

## Known Limitations

- No persistent user table, role table or server-side RBAC permissions are implemented yet.
- CSRF protection, rate limiting and a full audit trail for the new admin session are not implemented.
- Several sales, marketing, competitor and export routes remain structural or placeholder implementations.
- The in-memory `asyncio.Queue` is not a durable queue.
- The central admin dashboard is a working control surface, but it does not yet provide every requested enterprise module on its own page.
