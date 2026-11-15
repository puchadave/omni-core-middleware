# Admin API Mapping

## Purpose

Map administrative UI functions to existing Omni-Core API endpoints and identify implemented, placeholder, or missing functionality.

## Core Dashboard and System

| Admin area | Endpoint | Method | Status | Notes |
|---|---|---:|---|---|
| System overview | `/api/stats` | GET | Implemented | Live counters, queue, uptime, OmniRoute health. |
| Health | `/api/health` | GET | Implemented | Database, OmniRoute, queue, uptime. |
| Control center overview | `/api/control-center/overview` | GET | Implemented | Executive overview. |
| Control center dashboard | `/api/control-center/dashboard` | GET | Implemented | Aggregated dashboard data. |
| Control center system | `/api/control-center/system` | GET | Implemented | System intelligence metrics. |
| Logs | `/api/logs` | GET | Implemented | Structured logs. |
| Import runs | `/api/import-runs` | GET | Implemented | Recent imports. |
| Integration health | `/api/health/integrations` | GET | Implemented | Database, OmniRoute, Shopware, Google, event bus. |

## Products, Orders, Pricing

| Admin area | Endpoint | Method | Status | Notes |
|---|---|---:|---|---|
| Product list | `/api/products` | GET | Implemented | Bounded pagination. |
| Product create/update | `/api/products` | POST | Implemented | Idempotent manual product upsert. |
| Product detail | `/api/products/{sku}` | GET | Implemented | 404 if missing. |
| Bundles | `/api/bundles` | GET | Implemented | Bundle definitions. |
| Price calculation | `/api/pricing/calculate` | POST | Implemented | Supplier price to marketplace price. |
| Trend analysis | `/api/ai/trend-analysis` | POST | Implemented | Requires OmniRoute. |
| Orders | `/api/orders` | GET | Implemented | Bounded pagination. |
| Order create | `/api/orders` | POST | `202` | Queue fulfillment. |
| Sales overview | `/api/sales/overview` | GET | Placeholder | Returns static status. |
| Product scores | `/api/sales/product-scores` | GET | Placeholder | Returns static status. |
| Pricing recommendations | `/api/sales/pricing-recommendations` | POST | Placeholder | Static calculation. |

## Imports and Pipelines

| Admin area | Endpoint | Method | Status | Notes |
|---|---|---:|---|---|
| Matterhorn import | `/api/import/matterhorn` | POST | Implemented | Synchronous importer. |
| Import runs | `/api/import-runs` | GET | Implemented | Recent runs. |
| Matterhorn → Google pipeline | `/api/pipeline/matterhorn-to-google` | POST | Implemented | AI optimization + Google export. |
| Auto export/import | `/api/pipeline/auto-export-imported` | POST | Implemented | Export recently imported products. |

## Marketing and Sales

| Admin area | Endpoint | Method | Status | Notes |
|---|---|---:|---|---|
| Campaigns | `/api/marketing/campaigns` | GET | Implemented | DB query. |
| Campaign generate | `/api/marketing/campaigns/generate` | POST | Placeholder | Returns status only. |
| Product content generate | `/api/marketing/products/{product_id}/generate-content` | POST | Placeholder | Returns status only. |
| Product content get | `/api/marketing/products/{product_id}/content` | GET | Implemented | Reads channel content. |

## Export

| Admin area | Endpoint | Method | Status | Notes |
|---|---|---:|---|---|
| Export channels | `/api/export/channels` | GET | Implemented | Returns channel list. |
| Export status | `/api/export/{channel}` | GET | Implemented | Adapter status. |
| Export generate | `/api/export/{channel}/generate` | POST | Implemented | Returns status only. |
| Export status | `/api/export/{channel}/status` | GET | Implemented | Adapter status. |
| Export download | `/api/export/{channel}/download` | GET | Placeholder | Returns status only. |
| Shopify CSV | `/api/export/shopify.csv` | GET | Implemented | Streaming CSV. |
| Google Shopping XML | `/api/export/google-shopping.xml` | GET | Implemented | XML feed. |
| Google export API | `/api/google/export...` | Multiple | Implemented / partial | Queue, process, retry, sync, health. |

## Integrations and Infrastructure

| Area | Endpoint | Method | Status | Notes |
|---|---|---:|---|---|
| Shopware connections | `/api/shopware/connections`, `/api/shopware/connections/{conn_id}/test`, mappings | Multiple | Implemented | In-memory managers. |
| Shopware export | `/api/shopware/export`, queue, stats, process | Multiple | Implemented | Queue manager. |
| Shopware description / SEO | `/api/shopware/description/generate`, `/api/shopware/seo/generate` | POST | Implemented | Requires engines. |
| Competitor | search, match, price, providers, stats | Multiple | Implemented / partial | Placeholder pricing/match logic. |
| Google | accounts, export, queue, stats, process, retry, sync, health | Multiple | Implemented | DB + runtime. |
| OmniRoute | providers, models, rules, policies, select, cost, stats | Multiple | Implemented | Runtime + DB. |
| Control center | overview, dashboard, system | GET | Implemented | Global control. |
| Pipeline | Matterhorn → Google, auto-export | POST | Implemented | Pipeline orchestration. |

## Status Summary

- Implemented endpoints are backed by services or direct database calls.
- Several sales, marketing, and export endpoints are intentional placeholders and must not be presented as fully operational.
- The administrative frontend should not assume unimplemented endpoints are functional.

## OpenAPI

- FastAPI generates OpenAPI at `/openapi.json`.
- Interactive docs at `/docs`.
- Redoc at `/redoc`.
- The route inventory was introspected from the FastAPI app and matches the decorators in `main.py`.

## Security Mapping

- Internal API key required for all non-excluded routes.
- Excluded: `/api/health`, `/docs`, `/redoc`, `/openapi.json`.
- Secret keys masked in settings.
- No RBAC/user model yet.
