# OMNI-CORE / Shopware Architecture

## Roles (separation of concerns)
- **OMNI-CORE (central):** AI orchestration, PIM/canonical product data, Matterhorn integration, pricing/competitor/trend engines, omnichannel & marketplace orchestration, CRM orchestration, analytics, event bus, fulfillment orchestration, AI control plane.
- **Shopware (Commerce Execution Layer):** Storefront, cart/checkout, customers, orders, products/categories/prices/promotions, sales channels, Admin/Store API, webhooks, commerce events.

Shopware is not the AI/business-orchestration source of truth.

## Target communication
```text
                    OMNI-CORE (:8002)
                       │
              ┌────────┴────────┐
              │                 │
              ▼                 ▼
        Shopware API       Shopware Webhooks
              │   (auth, idempotent, validated)
              └────────┬────────┘
                       │
                       ▼
                  SHOPWARE (LXC)
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
       Database       Redis       Search
      (non-public)  (local-only) (compatible)
```

OMNI-CORE must not access Shopware's internal database directly. Communication only via API/webhook/event.

## Data flow (canonical)
```text
Matterhorn -> OMNI-CORE -> PIM/Canonical Model -> Validation -> AI Enrichment -> Pricing -> Marketplace Mapping -> Shopware API
```
Shopware receives only validated canonical data. No silent conflict overwrite; sync state tracked (NEW/PENDING/SYNCING/SYNCED/CHANGED/FAILED/RETRY/CONFLICT).

## Proxmox helper
`install-shopware-omni.sh` provisions a dedicated, reproducible Shopware LXC (idempotent, dry-run capable) and prepares OMNI-CORE integration (API/webhook hooks, secure config, monitoring/backup hooks). See `docs/omni-core-integration.md` and `docs/operations.md`.
