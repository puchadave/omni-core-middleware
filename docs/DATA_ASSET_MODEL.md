# Data and Asset Model – Omni-Core

## Purpose

Defines the proposed data model for the expansion and the migration plan for implementing it without duplicating existing product master data.

## Existing Persistence Baseline

- `database.py` defines `POSTGRES_SCHEMA` and `SQLITE_SCHEMA`.
- Schema is applied automatically by `init_db()` on startup.
- PostgreSQL uses JSONB and constraints; SQLite uses TEXT JSON and equivalent checks.
- Existing durable entities include products, bundles, orders, channel content, logs, imports, Google, Shopware, competitor, pricing, and OmniRoute entities.
- No separate migration framework exists.

## Asset Model

### `assets`

- `id TEXT PRIMARY KEY`
- `asset_type TEXT NOT NULL CHECK (asset_type IN ('image', 'video', 'document', 'audio', 'graphic'))`
- `storage_backend TEXT NOT NULL DEFAULT 'local'`
- `storage_path TEXT NOT NULL`
- `original_filename TEXT NOT NULL DEFAULT ''`
- `mime_type TEXT NOT NULL DEFAULT ''`
- `size_bytes BIGINT NOT NULL DEFAULT 0`
- `sha256_hash TEXT NOT NULL`
- `width INTEGER`
- `height INTEGER`
- `duration_seconds NUMERIC(10,3)`
- `source_type TEXT NOT NULL DEFAULT 'upload'`
- `source_url TEXT NOT NULL DEFAULT ''`
- `product_sku TEXT REFERENCES products(sku) ON DELETE SET NULL`
- `model_profile_id TEXT`
- `campaign_id TEXT`
- `rights_status TEXT NOT NULL DEFAULT 'unknown'`
- `approval_status TEXT NOT NULL DEFAULT 'draft'`
- `created_by TEXT NOT NULL DEFAULT 'system'`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- `updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

### `asset_versions`

- `id TEXT PRIMARY KEY`
- `asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE`
- `version_number INTEGER NOT NULL`
- `storage_path TEXT NOT NULL`
- `sha256_hash TEXT NOT NULL`
- `change_reason TEXT NOT NULL DEFAULT ''`
- `created_by TEXT NOT NULL DEFAULT 'system'`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- `UNIQUE(asset_id, version_number)`

Assets store metadata and references only. Original supplier images must never be overwritten by generated variants. Generated assets receive new IDs and versions.

## Model Identity Model

### `model_profiles`

- `id TEXT PRIMARY KEY`
- `display_name TEXT NOT NULL`
- `model_type TEXT NOT NULL CHECK (model_type IN ('own_identity', 'fictional', 'real_person'))`
- `active BOOLEAN NOT NULL DEFAULT TRUE`
- `visual_traits_json JSONB NOT NULL DEFAULT '{}'::jsonb`
- `pose_preferences_json JSONB NOT NULL DEFAULT '[]'::jsonb`
- `style_description TEXT NOT NULL DEFAULT ''`
- `permitted_use_json JSONB NOT NULL DEFAULT '[]'::jsonb`
- `consent_status TEXT NOT NULL DEFAULT 'not_required' CHECK (consent_status IN ('not_required', 'pending', 'granted', 'expired', 'revoked'))`
- `consent_valid_from TIMESTAMPTZ`
- `consent_valid_until TIMESTAMPTZ`
- `rights_holder TEXT NOT NULL DEFAULT ''`
- `approval_document_ref TEXT NOT NULL DEFAULT ''`
- `created_at`, `updated_at`

### `model_references`

- `id TEXT PRIMARY KEY`
- `model_profile_id TEXT NOT NULL REFERENCES model_profiles(id) ON DELETE CASCADE`
- `reference_type TEXT NOT NULL CHECK (reference_type IN ('front', 'profile', 'back', 'expression', 'full_body', 'pack'))`
- `asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE`
- `version_number INTEGER NOT NULL DEFAULT 1`
- `quality_score NUMERIC(5,2)`
- `is_primary BOOLEAN NOT NULL DEFAULT FALSE`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

### `model_permissions`

- `id TEXT PRIMARY KEY`
- `model_profile_id TEXT NOT NULL REFERENCES model_profiles(id) ON DELETE CASCADE`
- `permission_type TEXT NOT NULL`
- `granted BOOLEAN NOT NULL DEFAULT FALSE`
- `scope_json JSONB NOT NULL DEFAULT '{}'::jsonb`
- `valid_until TIMESTAMPTZ`
- `created_at`, `updated_at`

## Creative and Video Model

### `creative_projects`

- `id TEXT PRIMARY KEY`
- `name TEXT NOT NULL`
- `project_type TEXT NOT NULL CHECK (project_type IN ('photoshoot', 'campaign', 'social_content', 'video'))`
- `status TEXT NOT NULL DEFAULT 'draft'`
- `product_sku TEXT REFERENCES products(sku) ON DELETE SET NULL`
- `bundle_sku TEXT REFERENCES products(sku) ON DELETE SET NULL`
- `model_profile_id TEXT REFERENCES model_profiles(id) ON DELETE SET NULL`
- `created_by TEXT NOT NULL DEFAULT 'system'`
- `created_at`, `updated_at`

### `creative_jobs`

- `id TEXT PRIMARY KEY`
- `project_id TEXT NOT NULL REFERENCES creative_projects(id) ON DELETE CASCADE`
- `job_type TEXT NOT NULL`
- `status TEXT NOT NULL CHECK (status IN ('draft', 'generating', 'generated', 'quality_check', 'review_required', 'approved', 'rejected', 'published', 'archived', 'failed'))`
- `provider_id TEXT REFERENCES ai_providers(id) ON DELETE SET NULL`
- `model_id TEXT REFERENCES ai_models(id) ON DELETE SET NULL`
- `prompt_version TEXT NOT NULL DEFAULT ''`
- `request_payload_json JSONB NOT NULL DEFAULT '{}'::jsonb`
- `response_payload_json JSONB NOT NULL DEFAULT '{}'::jsonb`
- `progress_percent INTEGER NOT NULL DEFAULT 0`
- `cost_usd NUMERIC(10,6)`
- `error_message TEXT NOT NULL DEFAULT ''`
- `started_at TIMESTAMPTZ`
- `completed_at TIMESTAMPTZ`
- `created_at`, `updated_at`

### `creative_outputs`

- `id TEXT PRIMARY KEY`
- `job_id TEXT NOT NULL REFERENCES creative_jobs(id) ON DELETE CASCADE`
- `asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE`
- `output_type TEXT NOT NULL CHECK (output_type IN ('image', 'video', 'text', 'audio'))`
- `quality_score NUMERIC(5,2)`
- `fidelity_score NUMERIC(5,2)`
- `model_consistency_score NUMERIC(5,2)`
- `technical_score NUMERIC(5,2)`
- `approval_status TEXT NOT NULL DEFAULT 'review_required'`
- `rejection_reason TEXT NOT NULL DEFAULT ''`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

### `video_projects`

- `id TEXT PRIMARY KEY`
- `creative_project_id TEXT NOT NULL REFERENCES creative_projects(id) ON DELETE CASCADE`
- `format TEXT NOT NULL CHECK (format IN ('9:16', '16:9', '1:1', '4:5'))`
- `target_channel TEXT NOT NULL DEFAULT ''`
- `status TEXT NOT NULL DEFAULT 'draft'`
- `created_at`, `updated_at`

### `video_scenes`

- `id TEXT PRIMARY KEY`
- `video_project_id TEXT NOT NULL REFERENCES video_projects(id) ON DELETE CASCADE`
- `scene_order INTEGER NOT NULL`
- `scene_type TEXT NOT NULL`
- `prompt_text TEXT NOT NULL DEFAULT ''`
- `source_asset_id TEXT REFERENCES assets(id) ON DELETE SET NULL`
- `generated_asset_id TEXT REFERENCES assets(id) ON DELETE SET NULL`
- `duration_seconds NUMERIC(10,3)`
- `status TEXT NOT NULL DEFAULT 'draft'`
- `created_at`, `updated_at`

## Agent Model

### `agent_profiles`

- `id TEXT PRIMARY KEY`
- `name TEXT NOT NULL`
- `description TEXT NOT NULL DEFAULT ''`
- `allowed_tools_json JSONB NOT NULL DEFAULT '[]'::jsonb`
- `model_id TEXT REFERENCES ai_models(id) ON DELETE SET NULL`
- `system_prompt TEXT NOT NULL DEFAULT ''`
- `budget_usd NUMERIC(12,4) NOT NULL DEFAULT 0`
- `time_limit_seconds INTEGER NOT NULL DEFAULT 300`
- `is_active BOOLEAN NOT NULL DEFAULT TRUE`
- `created_at`, `updated_at`

### `agent_conversations`

- `id TEXT PRIMARY KEY`
- `agent_profile_id TEXT NOT NULL REFERENCES agent_profiles(id) ON DELETE CASCADE`
- `user_actor TEXT NOT NULL DEFAULT 'system'`
- `status TEXT NOT NULL DEFAULT 'active'`
- `created_at`, `updated_at`

### `agent_runs`

- `id TEXT PRIMARY KEY`
- `conversation_id TEXT NOT NULL REFERENCES agent_conversations(id) ON DELETE CASCADE`
- `agent_profile_id TEXT NOT NULL REFERENCES agent_profiles(id) ON DELETE CASCADE`
- `status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'paused', 'cancelled'))`
- `input_json JSONB NOT NULL DEFAULT '{}'::jsonb`
- `output_json JSONB NOT NULL DEFAULT '{}'::jsonb`
- `started_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- `completed_at TIMESTAMPTZ`
- `error_message TEXT NOT NULL DEFAULT ''`

### `agent_tool_calls`

- `id TEXT PRIMARY KEY`
- `run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE`
- `tool_name TEXT NOT NULL`
- `input_json JSONB NOT NULL DEFAULT '{}'::jsonb`
- `output_json JSONB NOT NULL DEFAULT '{}'::jsonb`
- `status TEXT NOT NULL CHECK (status IN ('success', 'error', 'rejected'))`
- `error_message TEXT NOT NULL DEFAULT ''`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

### `agent_approvals`

- `id TEXT PRIMARY KEY`
- `run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE`
- `target_type TEXT NOT NULL`
- `target_id TEXT NOT NULL`
- `approval_status TEXT NOT NULL CHECK (approval_status IN ('pending', 'approved', 'rejected'))`
- `reason TEXT NOT NULL DEFAULT ''`
- `reviewed_by TEXT NOT NULL DEFAULT ''`
- `reviewed_at TIMESTAMPTZ`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

## Commerce Intelligence Model

### `product_quality`

- `product_sku TEXT PRIMARY KEY REFERENCES products(sku) ON DELETE CASCADE`
- `attribute_score NUMERIC(5,2) NOT NULL DEFAULT 0`
- `description_score NUMERIC(5,2) NOT NULL DEFAULT 0`
- `variant_score NUMERIC(5,2) NOT NULL DEFAULT 0`
- `media_score NUMERIC(5,2) NOT NULL DEFAULT 0`
- `seo_score NUMERIC(5,2) NOT NULL DEFAULT 0`
- `platform_score NUMERIC(5,2) NOT NULL DEFAULT 0`
- `technical_score NUMERIC(5,2) NOT NULL DEFAULT 0`
- `total_score NUMERIC(5,2) NOT NULL DEFAULT 0`
- `issues_json JSONB NOT NULL DEFAULT '[]'::jsonb`
- `suggestions_json JSONB NOT NULL DEFAULT '[]'::jsonb`
- `updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

### `channel_readiness`

- `id TEXT PRIMARY KEY`
- `product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE`
- `channel TEXT NOT NULL`
- `score NUMERIC(5,2) NOT NULL DEFAULT 0`
- `errors_json JSONB NOT NULL DEFAULT '[]'::jsonb`
- `warnings_json JSONB NOT NULL DEFAULT '[]'::jsonb`
- `checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- `UNIQUE(product_sku, channel)`

### `supplier_profiles`

- `id TEXT PRIMARY KEY`
- `name TEXT NOT NULL`
- `source_type TEXT NOT NULL`
- `source_url TEXT NOT NULL DEFAULT ''`
- `is_active BOOLEAN NOT NULL DEFAULT TRUE`
- `created_at`, `updated_at`

### `supplier_metrics`

- `id TEXT PRIMARY KEY`
- `supplier_id TEXT NOT NULL REFERENCES supplier_profiles(id) ON DELETE CASCADE`
- `metric_name TEXT NOT NULL`
- `metric_value NUMERIC(16,4) NOT NULL`
- `recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

### `bundles_intelligence`

- `bundle_sku TEXT PRIMARY KEY REFERENCES products(sku) ON DELETE CASCADE`
- `style TEXT NOT NULL DEFAULT ''`
- `target_audience TEXT NOT NULL DEFAULT ''`
- `season TEXT NOT NULL DEFAULT ''`
- `quality_score NUMERIC(5,2) NOT NULL DEFAULT 0`
- `approval_status TEXT NOT NULL DEFAULT 'draft'`
- `created_at`, `updated_at`

### `bundles_intelligence_products`

- `bundle_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE`
- `product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE`
- `position INTEGER NOT NULL DEFAULT 0`
- `reason TEXT NOT NULL DEFAULT ''`
- `PRIMARY KEY(bundle_sku, product_sku)`

### `pricing_history`

- `id BIGSERIAL PRIMARY KEY`
- `product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE`
- `old_price NUMERIC(12,2) NOT NULL`
- `new_price NUMERIC(12,2) NOT NULL`
- `reason TEXT NOT NULL DEFAULT ''`
- `actor TEXT NOT NULL DEFAULT 'system'`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

### `demand_forecasts`

- `id TEXT PRIMARY KEY`
- `product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE`
- `forecast_period TEXT NOT NULL`
- `expected_demand NUMERIC(12,2) NOT NULL DEFAULT 0`
- `confidence NUMERIC(5,2) NOT NULL DEFAULT 0`
- `basis_json JSONB NOT NULL DEFAULT '{}'::jsonb`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

## Social Commerce Model

The social module should reuse existing product and asset IDs. Proposed tables:

- `social_accounts`
- `social_platform_connections`
- `social_permissions`
- `social_campaigns`
- `social_content`
- `social_content_versions`
- `social_media_assets`
- `social_editorial_calendar`
- `social_publishing_jobs`
- `social_publications`
- `social_analytics`
- `social_hashtags`
- `social_trends`
- `brand_profiles`
- `brand_voice_rules`
- `social_automation_rules`

These should reference `products(sku)` and `assets(id)` instead of duplicating product or media master data.

## Migration Plan

1. Add new tables only; do not modify existing product, order, or integration tables in the first pass.
2. Add equivalent PostgreSQL and SQLite DDL to `POSTGRES_SCHEMA` and `SQLITE_SCHEMA`.
3. Use `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`.
4. Add foreign keys to existing `products(sku)` and new `assets(id)` where ownership is clear.
5. Keep JSON fields as JSONB in PostgreSQL and TEXT in SQLite.
6. Add indexes for `product_sku`, `model_profile_id`, `asset_id`, `job_id`, `run_id`, and status fields.
7. Backfill no historical data in the first migration; new tables start empty.
8. Add service functions with dual backend branches, following the existing `uses_postgres()` pattern.
9. Add API routes with Pydantic schemas and internal API key protection.
10. Add tests using temporary SQLite or mocked providers.
11. Update OpenAPI and admin documentation after routes are implemented.
12. Do not mark any module complete until UI, backend, persistence, permissions, error handling, and tests exist.

## Initial Implementation Order

1. Asset management and Model Identity Manager.
2. OmniRoute agent integration and tool registry.
3. AI Creative Agent.
4. Photo Studio and Product Fidelity Engine.
5. Video Studio.
6. Creative approval and Campaign Studio.
7. Product Intelligence and Channel Readiness.
8. Bundle Intelligence.
9. Dynamic Pricing and Market Intelligence.
10. Supplier Intelligence and Demand Forecasting.
11. Operations Intelligence and Agent Manager.
12. Social Commerce.
13. Full tests and security review.
