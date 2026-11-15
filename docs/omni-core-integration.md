# OMNI-CORE ↔ Shopware Integration

## Configuration (Shopware node)
Prepared by installer in `/opt/omni-shopware-connector/config/connector.json` and secure secrets dir (never logged):
- `OMNI_CORE_ENABLED=true`
- `OMNI_CORE_BASE_URL` (e.g. `http://<omni-host>:8002`)
- `OMNI_CORE_API_KEY` / webhook secrets (stored with restrictive permissions)

## Communication
- **OMNI-CORE → Shopware:** Official Shopware Admin/Store APIs (least-privilege technical account, e.g. `OMNI_CORE`).
- **Shopware → OMNI-CORE:** Webhooks/events to `${OMNI_CORE_BASE_URL}/api/integrations/shopware/webhook` (authenticated, timestamp/replay-protected, idempotent, logged without secrets/PII).
- No direct DB access to Shopware internals.

## Canonical mapping (prepared)
SKU, EAN/GTIN, manufacturer, name/descriptions, categories/properties/variants, media, prices/tax/stock, dimensions/weight, custom fields, SEO/meta. Shopware-specific fields are not written back into canonical model unless defined.

## Suggested custom fields (idempotent create-if-missing)
`omni_product_id`, `omni_source`, `omni_source_id`, `omni_sync_status`, `omni_sync_version`, `omni_last_sync`, `omni_ai_enriched`, `omni_ai_version`, `omni_content_hash`, `omni_marketplace_status`, `omni_pricing_status`, `omni_media_status`.

## Sync & idempotency
Events/objects processed idempotently by (event/object/version). Conflicts surfaced, never silently overwritten. Integration marked `PENDING` if OMNI-CORE unreachable at install time (Shopware install still completes).

## Security
Authenticated webhooks, least privilege, Redis/DB/Search bound locally/non-public, TLS-ready/reverse-proxy support, secrets never printed or committed.
