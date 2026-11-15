"""Unified persistence layer for Omni-Core ERP & BI Middleware.

Production deployments use PostgreSQL through asyncpg. A SQLite WAL fallback is
kept for local development and edge installations. Public functions use the same
contract on either backend; write operations are short and transactional.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence

import aiosqlite

try:
    import asyncpg
except ImportError:  # Allows SQLite-only local development before dependencies install.
    asyncpg = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "middleware.db"))
BUSY_TIMEOUT_MS = 5_000
POSTGRES_RETRY_ATTEMPTS = 30
POSTGRES_RETRY_DELAY_SECONDS = 2
_pg_pool: Any | None = None


class DatabaseError(RuntimeError):
    """Raised when a persistent state transition cannot be completed."""


class InsufficientStockError(DatabaseError):
    """Raised when an order cannot reserve inventory without overselling."""


def uses_postgres() -> bool:
    """Return whether the configured backend is PostgreSQL."""
    return DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp suitable for both supported databases."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


POSTGRES_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT '',
        is_secret BOOLEAN NOT NULL DEFAULT FALSE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS products (
        sku TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '',
        raw_description TEXT NOT NULL DEFAULT '',
        ai_title TEXT NOT NULL DEFAULT '',
        ai_description_html TEXT NOT NULL DEFAULT '',
        ai_tags_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        supplier_price NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (supplier_price >= 0),
        stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
        is_bundle BOOLEAN NOT NULL DEFAULT FALSE,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('draft', 'active', 'paused', 'archived')),
        source_hash TEXT NOT NULL DEFAULT '',
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_products_status_stock ON products(status, stock, is_bundle)",
    "CREATE INDEX IF NOT EXISTS idx_products_ai_tags_json ON products USING GIN (ai_tags_json)",
    """
    CREATE TABLE IF NOT EXISTS bundles (
        bundle_sku TEXT PRIMARY KEY REFERENCES products(sku) ON DELETE CASCADE,
        component_skus_json JSONB NOT NULL,
        title TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bundles_components_json ON bundles USING GIN (component_skus_json)",
    """
    CREATE TABLE IF NOT EXISTS channel_content (
        id BIGSERIAL PRIMARY KEY,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
        channel TEXT NOT NULL CHECK (channel IN ('shopware', 'ebay', 'amazon', 'kaufland', 'etsy')),
        content_type TEXT NOT NULL CHECK (content_type IN ('title', 'description', 'tags', 'image')),
        content_text TEXT NOT NULL DEFAULT '',
        metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        ai_generated BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(product_sku, channel, content_type)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_channel_content_product_channel ON channel_content(product_sku, channel)",
    "CREATE INDEX IF NOT EXISTS idx_channel_content_metadata_json ON channel_content USING GIN (metadata_json)",
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        sku TEXT NOT NULL REFERENCES products(sku),
        quantity INTEGER NOT NULL CHECK (quantity > 0),
        marketplace TEXT NOT NULL,
        customer_reference TEXT NOT NULL DEFAULT '',
        payment_status TEXT NOT NULL DEFAULT 'pending' CHECK (payment_status IN ('pending', 'paid', 'failed', 'refunded')),
        fulfillment_locked BOOLEAN NOT NULL DEFAULT FALSE,
        status TEXT NOT NULL DEFAULT 'received' CHECK (status IN ('received', 'reserved', 'dispatching', 'dispatched', 'failed', 'cancelled')),
        tracking_number TEXT NOT NULL DEFAULT '',
        tracking_url TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_orders_status_updated ON orders(status, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS bi_metrics (
        id BIGSERIAL PRIMARY KEY,
        metric_name TEXT NOT NULL,
        metric_value NUMERIC(16,4) NOT NULL,
        metric_label TEXT NOT NULL DEFAULT '',
        dimensions_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bi_metrics_name_time ON bi_metrics(metric_name, recorded_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_bi_metrics_dimensions_json ON bi_metrics USING GIN (dimensions_json)",
    """
    CREATE TABLE IF NOT EXISTS system_logs (
        id BIGSERIAL PRIMARY KEY,
        level TEXT NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR')),
        event TEXT NOT NULL,
        message TEXT NOT NULL,
        context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_system_logs_created_at ON system_logs(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_system_logs_context_json ON system_logs USING GIN (context_json)",
    """
    CREATE TABLE IF NOT EXISTS import_runs (
        id BIGSERIAL PRIMARY KEY,
        source TEXT NOT NULL,
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at TIMESTAMPTZ,
        imported INTEGER NOT NULL DEFAULT 0,
        updated INTEGER NOT NULL DEFAULT 0,
        errors INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'success', 'failed')),
        error_message TEXT NOT NULL DEFAULT ''
    )
    """,
    # ── Google Merchant Center Tables ────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS google_accounts (
        id TEXT PRIMARY KEY,
        merchant_id TEXT NOT NULL UNIQUE,
        account_name TEXT NOT NULL,
        google_cloud_project_id TEXT NOT NULL,
        account_status TEXT NOT NULL DEFAULT 'pending' CHECK (account_status IN ('pending', 'active', 'suspended', 'disconnected')),
        is_primary BOOLEAN NOT NULL DEFAULT FALSE,
        oauth_token_encrypted TEXT NOT NULL,
        oauth_refresh_token_encrypted TEXT,
        service_account_encrypted TEXT,
        scopes_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        token_expires_at TIMESTAMPTZ,
        last_token_validation TIMESTAMPTZ,
        last_token_status TEXT NOT NULL DEFAULT 'unknown',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_credentials (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
        credential_type TEXT NOT NULL CHECK (credential_type IN ('oauth2', 'service_account')),
        credential_name TEXT NOT NULL,
        encrypted_value TEXT NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_data_sources (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
        data_source_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        source_type TEXT NOT NULL DEFAULT 'products',
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'error')),
        primary_country TEXT NOT NULL DEFAULT 'DE',
        primary_language TEXT NOT NULL DEFAULT 'de',
        primary_currency TEXT NOT NULL DEFAULT 'EUR',
        feed_label TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(account_id, data_source_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT '',
        is_secret BOOLEAN NOT NULL DEFAULT FALSE,
        category TEXT NOT NULL DEFAULT 'general' CHECK (category IN ('general', 'export', 'sync', 'targeting', 'validation')),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_product_mappings (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
        matterhorn_field TEXT NOT NULL,
        google_field TEXT NOT NULL,
        mapping_type TEXT NOT NULL CHECK (mapping_type IN ('direct', 'transform', 'fallback', 'conditional', 'concatenate', 'clean')),
        mapping_config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        priority INTEGER NOT NULL DEFAULT 0,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_product_exports (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
        google_product_id TEXT NOT NULL DEFAULT '',
        offer_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'validation', 'exporting', 'completed', 'error', 'withdrawn', 'retry')),
        payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        response_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        error_message TEXT NOT NULL DEFAULT '',
        retry_count INTEGER NOT NULL DEFAULT 0,
        max_retries INTEGER NOT NULL DEFAULT 3,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        UNIQUE(product_sku, offer_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_sync_jobs (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
        job_type TEXT NOT NULL CHECK (job_type IN ('inventory', 'price', 'full_sync', 'product_update', 'withdrawal')),
        status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'error')),
        total_products INTEGER NOT NULL DEFAULT 0,
        processed_products INTEGER NOT NULL DEFAULT 0,
        failed_products INTEGER NOT NULL DEFAULT 0,
        error_message TEXT NOT NULL DEFAULT '',
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_sync_errors (
        id BIGSERIAL PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
        error_type TEXT NOT NULL,
        error_code TEXT NOT NULL DEFAULT '',
        error_message TEXT NOT NULL DEFAULT '',
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        resolved BOOLEAN NOT NULL DEFAULT FALSE,
        resolved_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_product_diagnostics (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
        google_product_id TEXT NOT NULL DEFAULT '',
        diagnosis_status TEXT NOT NULL CHECK (diagnosis_status IN ('active', 'disapproved', 'pending', 'limited')),
        issues_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        last_checked TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_performance_metrics (
        id BIGSERIAL PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
        metric_date DATE NOT NULL,
        impressions INTEGER NOT NULL DEFAULT 0,
        clicks INTEGER NOT NULL DEFAULT 0,
        ctr NUMERIC(6,4) NOT NULL DEFAULT 0,
        cpc NUMERIC(10,4) NOT NULL DEFAULT 0,
        cost NUMERIC(12,4) NOT NULL DEFAULT 0,
        conversions INTEGER NOT NULL DEFAULT 0,
        conversion_value NUMERIC(12,4) NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_audit_logs (
        id BIGSERIAL PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
        action TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT 'system',
        target_type TEXT NOT NULL DEFAULT '',
        target_id TEXT NOT NULL DEFAULT '',
        details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # ── OmniRoute Control Center Tables ─────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ai_providers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        provider_type TEXT NOT NULL CHECK (provider_type IN ('text', 'image', 'video', 'embedding', 'multimodal')),
        api_endpoint TEXT NOT NULL,
        api_key_encrypted TEXT NOT NULL,
        auth_type TEXT NOT NULL DEFAULT 'bearer' CHECK (auth_type IN ('bearer', 'api_key', 'oauth2')),
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        priority INTEGER NOT NULL DEFAULT 100,
        rate_limit_per_minute INTEGER NOT NULL DEFAULT 60,
        timeout_seconds INTEGER NOT NULL DEFAULT 30,
        max_retries INTEGER NOT NULL DEFAULT 3,
        retry_delay_seconds INTEGER NOT NULL DEFAULT 1,
        circuit_breaker_threshold INTEGER NOT NULL DEFAULT 5,
        circuit_breaker_timeout_seconds INTEGER NOT NULL DEFAULT 300,
        capabilities_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        health_status TEXT NOT NULL DEFAULT 'unknown' CHECK (health_status IN ('healthy', 'degraded', 'unhealthy', 'unknown')),
        last_health_check TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_provider_credentials (
        id TEXT PRIMARY KEY,
        provider_id TEXT NOT NULL REFERENCES ai_providers(id) ON DELETE CASCADE,
        credential_name TEXT NOT NULL,
        encrypted_value TEXT NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_models (
        id TEXT PRIMARY KEY,
        provider_id TEXT NOT NULL REFERENCES ai_providers(id) ON DELETE CASCADE,
        model_id TEXT NOT NULL,
        model_name TEXT NOT NULL,
        model_version TEXT NOT NULL DEFAULT '',
        model_category TEXT NOT NULL DEFAULT 'general' CHECK (model_category IN ('text', 'image', 'video', 'embedding', 'multimodal')),
        capabilities_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        context_window INTEGER NOT NULL DEFAULT 4096,
        input_price_per_1k NUMERIC(10,6) NOT NULL DEFAULT 0,
        output_price_per_1k NUMERIC(10,6) NOT NULL DEFAULT 0,
        image_price_per_image NUMERIC(10,6) NOT NULL DEFAULT 0,
        rate_limit_per_minute INTEGER NOT NULL DEFAULT 60,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        is_discoverable BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(provider_id, model_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_model_capabilities (
        id TEXT PRIMARY KEY,
        model_id TEXT NOT NULL REFERENCES ai_models(id) ON DELETE CASCADE,
        capability_name TEXT NOT NULL,
        capability_value TEXT NOT NULL DEFAULT '',
        confidence NUMERIC(3,2) NOT NULL DEFAULT 1.0,
        source TEXT NOT NULL DEFAULT 'discovered' CHECK (source IN ('discovered', 'manual')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_routing_rules (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        strategy TEXT NOT NULL CHECK (strategy IN ('lowest_cost', 'highest_quality', 'lowest_latency', 'provider_priority', 'capability_matching', 'weighted_distribution')),
        provider_priority_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        model_category TEXT NOT NULL DEFAULT 'general',
        min_quality_score NUMERIC(3,2) NOT NULL DEFAULT 0.0,
        max_cost_per_1k NUMERIC(10,6),
        max_latency_ms INTEGER,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_routing_policies (
        id TEXT PRIMARY KEY,
        workflow_name TEXT NOT NULL,
        rule_id TEXT NOT NULL REFERENCES ai_routing_rules(id) ON DELETE CASCADE,
        fallback_rule_id TEXT REFERENCES ai_routing_rules(id),
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(workflow_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_workflows (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        trigger_event TEXT NOT NULL DEFAULT '',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        max_executions_per_hour INTEGER NOT NULL DEFAULT 1000,
        budget_per_execution NUMERIC(10,4) NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_workflow_configs (
        id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL REFERENCES ai_workflows(id) ON DELETE CASCADE,
        step_name TEXT NOT NULL,
        model_id TEXT REFERENCES ai_models(id),
        provider_id TEXT REFERENCES ai_providers(id),
        prompt_template TEXT NOT NULL DEFAULT '',
        parameters_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        step_order INTEGER NOT NULL DEFAULT 0,
        is_required BOOLEAN NOT NULL DEFAULT TRUE,
        on_failure TEXT NOT NULL DEFAULT 'stop' CHECK (on_failure IN ('stop', 'continue', 'retry', 'fallback')),
        max_retries INTEGER NOT NULL DEFAULT 3,
        timeout_seconds INTEGER NOT NULL DEFAULT 30,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_usage_records (
        id BIGSERIAL PRIMARY KEY,
        provider_id TEXT NOT NULL REFERENCES ai_providers(id),
        model_id TEXT REFERENCES ai_models(id),
        workflow_id TEXT REFERENCES ai_workflows(id),
        request_type TEXT NOT NULL,
        tokens_input INTEGER NOT NULL DEFAULT 0,
        tokens_output INTEGER NOT NULL DEFAULT 0,
        images_generated INTEGER NOT NULL DEFAULT 0,
        videos_generated INTEGER NOT NULL DEFAULT 0,
        cost_usd NUMERIC(10,6) NOT NULL DEFAULT 0,
        latency_ms INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'success' CHECK (status IN ('success', 'error', 'retry', 'fallback')),
        error_message TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_cost_records (
        id BIGSERIAL PRIMARY KEY,
        provider_id TEXT NOT NULL REFERENCES ai_providers(id),
        model_id TEXT REFERENCES ai_models(id),
        workflow_id TEXT REFERENCES ai_workflows(id),
        date DATE NOT NULL,
        total_cost_usd NUMERIC(12,4) NOT NULL DEFAULT 0,
        request_count INTEGER NOT NULL DEFAULT 0,
        token_count INTEGER NOT NULL DEFAULT 0,
        image_count INTEGER NOT NULL DEFAULT 0,
        video_count INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_budgets (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        budget_type TEXT NOT NULL CHECK (budget_type IN ('daily', 'monthly', 'provider', 'workflow')),
        target_id TEXT NOT NULL DEFAULT '',
        amount_usd NUMERIC(12,4) NOT NULL DEFAULT 0,
        warning_threshold_percent INTEGER NOT NULL DEFAULT 80,
        action_on_exceed TEXT NOT NULL DEFAULT 'warn' CHECK (action_on_exceed IN ('warn', 'pause', 'fallback', 'block')),
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        reset_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_provider_health (
        id BIGSERIAL PRIMARY KEY,
        provider_id TEXT NOT NULL REFERENCES ai_providers(id),
        status TEXT NOT NULL CHECK (status IN ('healthy', 'degraded', 'unhealthy', 'unknown')),
        response_time_ms INTEGER NOT NULL DEFAULT 0,
        error_rate NUMERIC(5,4) NOT NULL DEFAULT 0,
        last_check TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        details_json JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_request_logs (
        id BIGSERIAL PRIMARY KEY,
        provider_id TEXT NOT NULL REFERENCES ai_providers(id),
        model_id TEXT REFERENCES ai_models(id),
        workflow_id TEXT REFERENCES ai_workflows(id),
        request_id TEXT NOT NULL,
        prompt_hash TEXT NOT NULL DEFAULT '',
        request_payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        response_payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        latency_ms INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'success',
        error_message TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_error_logs (
        id BIGSERIAL PRIMARY KEY,
        provider_id TEXT REFERENCES ai_providers(id),
        model_id TEXT REFERENCES ai_models(id),
        workflow_id TEXT REFERENCES ai_workflows(id),
        error_type TEXT NOT NULL,
        error_code TEXT NOT NULL DEFAULT '',
        error_message TEXT NOT NULL DEFAULT '',
        severity TEXT NOT NULL DEFAULT 'error' CHECK (severity IN ('debug', 'info', 'warning', 'error', 'critical')),
        resolved BOOLEAN NOT NULL DEFAULT FALSE,
        resolved_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_audit_logs (
        id BIGSERIAL PRIMARY KEY,
        actor TEXT NOT NULL DEFAULT 'system',
        action TEXT NOT NULL,
        target_type TEXT NOT NULL DEFAULT '',
        target_id TEXT NOT NULL DEFAULT '',
        details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shopware_connections (
        id TEXT PRIMARY KEY,
        shop_name TEXT NOT NULL,
        shop_url TEXT NOT NULL,
        shopware_version TEXT NOT NULL DEFAULT '6.0',
        api_client_id TEXT NOT NULL,
        api_client_secret_encrypted TEXT NOT NULL,
        sales_channel_id TEXT NOT NULL DEFAULT '',
        default_language TEXT NOT NULL DEFAULT 'de-DE',
        default_currency TEXT NOT NULL DEFAULT 'EUR',
        default_tax_rate NUMERIC(5,2) NOT NULL DEFAULT 19.00,
        default_supplier TEXT NOT NULL DEFAULT '',
        default_shipping_config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        last_health_check TIMESTAMPTZ,
        last_health_status TEXT NOT NULL DEFAULT 'unknown',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shopware_product_mappings (
        id TEXT PRIMARY KEY,
        connection_id TEXT NOT NULL REFERENCES shopware_connections(id) ON DELETE CASCADE,
        matterhorn_field TEXT NOT NULL,
        shopware_field TEXT NOT NULL,
        mapping_type TEXT NOT NULL CHECK (mapping_type IN ('direct', 'transform', 'fallback', 'conditional', 'concatenate', 'clean', 'translate')),
        mapping_config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        priority INTEGER NOT NULL DEFAULT 0,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        version INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(connection_id, matterhorn_field, shopware_field, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shopware_export_queue (
        id TEXT PRIMARY KEY,
        connection_id TEXT NOT NULL REFERENCES shopware_connections(id) ON DELETE CASCADE,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
        export_mode TEXT NOT NULL CHECK (export_mode IN ('publish', 'update', 'variant_update', 'price_update', 'stock_update', 'image_update', 'description_update', 'seo_update', 'category_update', 'full_sync')),
        status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'validation', 'release_required', 'exporting', 'completed', 'error', 'retry')),
        payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        response_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        error_message TEXT NOT NULL DEFAULT '',
        retry_count INTEGER NOT NULL DEFAULT 0,
        max_retries INTEGER NOT NULL DEFAULT 3,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS competitors (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        domain TEXT NOT NULL,
        shop_url TEXT NOT NULL DEFAULT '',
        marketplace TEXT NOT NULL DEFAULT '',
        country TEXT NOT NULL DEFAULT 'DE',
        currency TEXT NOT NULL DEFAULT 'EUR',
        detected_categories_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        detected_brands_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        last_successful_research TIMESTAMPTZ,
        products_found INTEGER NOT NULL DEFAULT 0,
        average_price NUMERIC(12,2),
        price_positioning TEXT NOT NULL DEFAULT 'unknown' CHECK (price_positioning IN ('cheap', 'average', 'expensive', 'unknown')),
        availability_status TEXT NOT NULL DEFAULT 'unknown' CHECK (availability_status IN ('good', 'limited', 'poor', 'unknown')),
        data_source TEXT NOT NULL DEFAULT '',
        reliability_score NUMERIC(3,2) NOT NULL DEFAULT 0.0 CHECK (reliability_score >= 0 AND reliability_score <= 1.0),
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS competitor_products (
        id TEXT PRIMARY KEY,
        competitor_id TEXT NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
        source_url TEXT NOT NULL,
        source_name TEXT NOT NULL DEFAULT '',
        ean TEXT NOT NULL DEFAULT '',
        sku TEXT NOT NULL DEFAULT '',
        product_name TEXT NOT NULL DEFAULT '',
        brand TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT '',
        price NUMERIC(12,2) NOT NULL DEFAULT 0,
        shipping_cost NUMERIC(10,2) NOT NULL DEFAULT 0,
        total_price NUMERIC(12,2) NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'EUR',
        availability TEXT NOT NULL DEFAULT '',
        last_checked TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        data_source TEXT NOT NULL DEFAULT '',
        match_score INTEGER NOT NULL DEFAULT 0 CHECK (match_score >= 0 AND match_score <= 100),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS product_matches (
        id TEXT PRIMARY KEY,
        own_product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
        competitor_product_id TEXT NOT NULL REFERENCES competitor_products(id) ON DELETE CASCADE,
        match_score INTEGER NOT NULL CHECK (match_score >= 0 AND match_score <= 100),
        match_stage TEXT NOT NULL CHECK (match_stage IN ('exact_ean', 'sku_model', 'brand_name_attrs', 'image_similarity', 'semantic', 'manual')),
        match_reason TEXT NOT NULL DEFAULT '',
        ai_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
        ai_confidence NUMERIC(3,2),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS competitor_price_history (
        id BIGSERIAL PRIMARY KEY,
        competitor_product_id TEXT NOT NULL REFERENCES competitor_products(id) ON DELETE CASCADE,
        price NUMERIC(12,2) NOT NULL DEFAULT 0,
        shipping_cost NUMERIC(10,2) NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'EUR',
        availability TEXT NOT NULL DEFAULT '',
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        data_source TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS monitoring_jobs (
        id TEXT PRIMARY KEY,
        competitor_id TEXT NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
        competitor_product_id TEXT REFERENCES competitor_products(id),
        interval_hours INTEGER NOT NULL CHECK (interval_hours IN (6, 12, 24, 168)),
        next_run TIMESTAMPTZ NOT NULL,
        last_run TIMESTAMPTZ,
        last_price NUMERIC(12,2),
        last_availability TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'error', 'disabled')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_alerts (
        id TEXT PRIMARY KEY,
        alert_type TEXT NOT NULL CHECK (alert_type IN ('competitor_lower', 'competitor_higher', 'out_of_stock', 'new_competitor', 'price_above_market', 'margin_below_min', 'special_offer')),
        own_product_sku TEXT REFERENCES products(sku) ON DELETE CASCADE,
        competitor_id TEXT REFERENCES competitors(id) ON DELETE CASCADE,
        competitor_product_id TEXT REFERENCES competitor_products(id) ON DELETE CASCADE,
        threshold_value NUMERIC(12,2),
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        last_triggered TIMESTAMPTZ,
        notification_channels_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pricing_recommendations (
        id TEXT PRIMARY KEY,
        own_product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
        current_price NUMERIC(12,2) NOT NULL DEFAULT 0,
        recommended_price NUMERIC(12,2) NOT NULL DEFAULT 0,
        market_average NUMERIC(12,2) NOT NULL DEFAULT 0,
        market_min NUMERIC(12,2) NOT NULL DEFAULT 0,
        expected_margin NUMERIC(5,4) NOT NULL DEFAULT 0,
        contribution_margin NUMERIC(5,4) NOT NULL DEFAULT 0,
        price_deviation NUMERIC(5,4) NOT NULL DEFAULT 0,
        strategy TEXT NOT NULL CHECK (strategy IN ('premium_positioning', 'market_average', 'competitive_pricing', 'margin_optimization', 'clearance')),
        justification TEXT NOT NULL DEFAULT '',
        data_quality TEXT NOT NULL DEFAULT '',
        risk_indicator TEXT NOT NULL DEFAULT '',
        is_approved BOOLEAN NOT NULL DEFAULT FALSE,
        applied_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_runs (
        id TEXT PRIMARY KEY,
        query TEXT NOT NULL,
        provider TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
        products_found INTEGER NOT NULL DEFAULT 0,
        matches_created INTEGER NOT NULL DEFAULT 0,
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at TIMESTAMPTZ,
        error_message TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pricing_rules (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        rule_type TEXT NOT NULL CHECK (rule_type IN ('global', 'brand', 'category', 'product', 'sales_channel')),
        target_entity_id TEXT NOT NULL DEFAULT '',
        min_margin NUMERIC(5,4) NOT NULL DEFAULT 0,
        target_margin NUMERIC(5,4),
        markup_percent NUMERIC(5,2),
        min_selling_price NUMERIC(12,2),
        max_selling_price NUMERIC(12,2),
        strategy TEXT NOT NULL DEFAULT 'market_average',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assets (
        id TEXT PRIMARY KEY,
        asset_type TEXT NOT NULL CHECK (asset_type IN ('image', 'video', 'document', 'audio', 'graphic')),
        storage_backend TEXT NOT NULL DEFAULT 'local',
        storage_path TEXT NOT NULL,
        original_filename TEXT NOT NULL DEFAULT '',
        mime_type TEXT NOT NULL DEFAULT '',
        size_bytes BIGINT NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
        sha256_hash TEXT NOT NULL,
        width INTEGER,
        height INTEGER,
        duration_seconds NUMERIC(10,3),
        source_type TEXT NOT NULL DEFAULT 'upload',
        source_url TEXT NOT NULL DEFAULT '',
        product_sku TEXT REFERENCES products(sku) ON DELETE SET NULL,
        model_profile_id TEXT,
        campaign_id TEXT,
        rights_status TEXT NOT NULL DEFAULT 'unknown' CHECK (rights_status IN ('unknown', 'owned', 'licensed', 'consent_granted', 'restricted')),
        approval_status TEXT NOT NULL DEFAULT 'draft' CHECK (approval_status IN ('draft', 'generating', 'generated', 'quality_check', 'review_required', 'approved', 'rejected', 'published', 'archived')),
        created_by TEXT NOT NULL DEFAULT 'system',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_assets_product_sku ON assets(product_sku)",
    "CREATE INDEX IF NOT EXISTS idx_assets_model_profile_id ON assets(model_profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_assets_approval_status ON assets(approval_status)",
    "CREATE INDEX IF NOT EXISTS idx_assets_sha256_hash ON assets(sha256_hash)",
    """
    CREATE TABLE IF NOT EXISTS asset_versions (
        id TEXT PRIMARY KEY,
        asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
        version_number INTEGER NOT NULL CHECK (version_number > 0),
        storage_path TEXT NOT NULL,
        sha256_hash TEXT NOT NULL,
        change_reason TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT 'system',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(asset_id, version_number)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_asset_versions_asset_id ON asset_versions(asset_id)",
    """
    CREATE TABLE IF NOT EXISTS model_profiles (
        id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        model_type TEXT NOT NULL CHECK (model_type IN ('own_identity', 'fictional', 'real_person')),
        active BOOLEAN NOT NULL DEFAULT TRUE,
        visual_traits_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        pose_preferences_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        style_description TEXT NOT NULL DEFAULT '',
        permitted_use_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        consent_status TEXT NOT NULL DEFAULT 'not_required' CHECK (consent_status IN ('not_required', 'pending', 'granted', 'expired', 'revoked')),
        consent_valid_from TIMESTAMPTZ,
        consent_valid_until TIMESTAMPTZ,
        rights_holder TEXT NOT NULL DEFAULT '',
        approval_document_ref TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_model_profiles_model_type ON model_profiles(model_type)",
    "CREATE INDEX IF NOT EXISTS idx_model_profiles_consent_status ON model_profiles(consent_status)",
    """
    CREATE TABLE IF NOT EXISTS model_references (
        id TEXT PRIMARY KEY,
        model_profile_id TEXT NOT NULL REFERENCES model_profiles(id) ON DELETE CASCADE,
        reference_type TEXT NOT NULL CHECK (reference_type IN ('front', 'profile', 'back', 'expression', 'full_body', 'pack')),
        asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
        version_number INTEGER NOT NULL DEFAULT 1 CHECK (version_number > 0),
        quality_score NUMERIC(5,2),
        is_primary BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_model_references_profile_id ON model_references(model_profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_model_references_asset_id ON model_references(asset_id)",
    """
    CREATE TABLE IF NOT EXISTS model_permissions (
        id TEXT PRIMARY KEY,
        model_profile_id TEXT NOT NULL REFERENCES model_profiles(id) ON DELETE CASCADE,
        permission_type TEXT NOT NULL,
        granted BOOLEAN NOT NULL DEFAULT FALSE,
        scope_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        valid_until TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(model_profile_id, permission_type)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_model_permissions_profile_id ON model_permissions(model_profile_id)",
)

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    is_secret INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0, 1)),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    raw_description TEXT NOT NULL DEFAULT '',
    ai_title TEXT NOT NULL DEFAULT '',
    ai_description_html TEXT NOT NULL DEFAULT '',
    ai_tags_json TEXT NOT NULL DEFAULT '[]',
    supplier_price REAL NOT NULL DEFAULT 0 CHECK (supplier_price >= 0),
    stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
    is_bundle INTEGER NOT NULL DEFAULT 0 CHECK (is_bundle IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('draft', 'active', 'paused', 'archived')),
    source_hash TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_status_stock ON products(status, stock, is_bundle);
CREATE TABLE IF NOT EXISTS bundles (
    bundle_sku TEXT PRIMARY KEY REFERENCES products(sku) ON DELETE CASCADE,
    component_skus_json TEXT NOT NULL,
    title TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    content_type TEXT NOT NULL,
    content_text TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    ai_generated INTEGER NOT NULL DEFAULT 0 CHECK (ai_generated IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(product_sku, channel, content_type)
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL REFERENCES products(sku),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    marketplace TEXT NOT NULL,
    customer_reference TEXT NOT NULL DEFAULT '',
    payment_status TEXT NOT NULL DEFAULT 'pending' CHECK (payment_status IN ('pending', 'paid', 'failed', 'refunded')),
    fulfillment_locked INTEGER NOT NULL DEFAULT 0 CHECK (fulfillment_locked IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'received' CHECK (status IN ('received', 'reserved', 'dispatching', 'dispatched', 'failed', 'cancelled')),
    tracking_number TEXT NOT NULL DEFAULT '',
    tracking_url TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status_updated ON orders(status, updated_at DESC);
CREATE TABLE IF NOT EXISTS bi_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    metric_label TEXT NOT NULL DEFAULT '',
    dimensions_json TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR')),
    event TEXT NOT NULL,
    message TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_system_logs_created_at ON system_logs(created_at DESC);
CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    imported INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'success', 'failed')),
    error_message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS shopware_connections (
    id TEXT PRIMARY KEY,
    shop_name TEXT NOT NULL,
    shop_url TEXT NOT NULL,
    shopware_version TEXT NOT NULL DEFAULT '6.0',
    api_client_id TEXT NOT NULL,
    api_client_secret_encrypted TEXT NOT NULL,
    sales_channel_id TEXT NOT NULL DEFAULT '',
    default_language TEXT NOT NULL DEFAULT 'de-DE',
    default_currency TEXT NOT NULL DEFAULT 'EUR',
    default_tax_rate REAL NOT NULL DEFAULT 19.00,
    default_supplier TEXT NOT NULL DEFAULT '',
    default_shipping_config_json TEXT NOT NULL DEFAULT '{}',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    last_health_check TEXT,
    last_health_status TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shopware_product_mappings (
    id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL,
    matterhorn_field TEXT NOT NULL,
    shopware_field TEXT NOT NULL,
    mapping_type TEXT NOT NULL CHECK (mapping_type IN ('direct', 'transform', 'fallback', 'conditional', 'concatenate', 'clean', 'translate')),
    mapping_config_json TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shopware_export_queue (
    id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL,
    product_sku TEXT NOT NULL,
    export_mode TEXT NOT NULL CHECK (export_mode IN ('publish', 'update', 'variant_update', 'price_update', 'stock_update', 'image_update', 'description_update', 'seo_update', 'category_update', 'full_sync')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'validation', 'release_required', 'exporting', 'completed', 'error', 'retry')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    response_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS competitors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    shop_url TEXT NOT NULL DEFAULT '',
    marketplace TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT 'DE',
    currency TEXT NOT NULL DEFAULT 'EUR',
    detected_categories_json TEXT NOT NULL DEFAULT '[]',
    detected_brands_json TEXT NOT NULL DEFAULT '[]',
    last_successful_research TEXT,
    products_found INTEGER NOT NULL DEFAULT 0,
    average_price REAL,
    price_positioning TEXT NOT NULL DEFAULT 'unknown' CHECK (price_positioning IN ('cheap', 'average', 'expensive', 'unknown')),
    availability_status TEXT NOT NULL DEFAULT 'unknown' CHECK (availability_status IN ('good', 'limited', 'poor', 'unknown')),
    data_source TEXT NOT NULL DEFAULT '',
    reliability_score REAL NOT NULL DEFAULT 0.0 CHECK (reliability_score >= 0 AND reliability_score <= 1.0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS competitor_products (
    id TEXT PRIMARY KEY,
    competitor_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_name TEXT NOT NULL DEFAULT '',
    ean TEXT NOT NULL DEFAULT '',
    sku TEXT NOT NULL DEFAULT '',
    product_name TEXT NOT NULL DEFAULT '',
    brand TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    price REAL NOT NULL DEFAULT 0,
    shipping_cost REAL NOT NULL DEFAULT 0,
    total_price REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'EUR',
    availability TEXT NOT NULL DEFAULT '',
    last_checked TEXT NOT NULL DEFAULT '',
    data_source TEXT NOT NULL DEFAULT '',
    match_score INTEGER NOT NULL DEFAULT 0 CHECK (match_score >= 0 AND match_score <= 100),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_matches (
    id TEXT PRIMARY KEY,
    own_product_sku TEXT NOT NULL,
    competitor_product_id TEXT NOT NULL,
    match_score INTEGER NOT NULL CHECK (match_score >= 0 AND match_score <= 100),
    match_stage TEXT NOT NULL CHECK (match_stage IN ('exact_ean', 'sku_model', 'brand_name_attrs', 'image_similarity', 'semantic', 'manual')),
    match_reason TEXT NOT NULL DEFAULT '',
    ai_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (ai_confirmed IN (0, 1)),
    ai_confidence REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS competitor_price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_product_id TEXT NOT NULL,
    price REAL NOT NULL DEFAULT 0,
    shipping_cost REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'EUR',
    availability TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL DEFAULT '',
    data_source TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS monitoring_jobs (
    id TEXT PRIMARY KEY,
    competitor_id TEXT NOT NULL,
    competitor_product_id TEXT,
    interval_hours INTEGER NOT NULL CHECK (interval_hours IN (6, 12, 24, 168)),
    next_run TEXT NOT NULL,
    last_run TEXT,
    last_price REAL,
    last_availability TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'error', 'disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_alerts (
    id TEXT PRIMARY KEY,
    alert_type TEXT NOT NULL CHECK (alert_type IN ('competitor_lower', 'competitor_higher', 'out_of_stock', 'new_competitor', 'price_above_market', 'margin_below_min', 'special_offer')),
    own_product_sku TEXT,
    competitor_id TEXT,
    competitor_product_id TEXT,
    threshold_value REAL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    last_triggered TEXT,
    notification_channels_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pricing_recommendations (
    id TEXT PRIMARY KEY,
    own_product_sku TEXT NOT NULL,
    current_price REAL NOT NULL DEFAULT 0,
    recommended_price REAL NOT NULL DEFAULT 0,
    market_average REAL NOT NULL DEFAULT 0,
    market_min REAL NOT NULL DEFAULT 0,
    expected_margin REAL NOT NULL DEFAULT 0,
    contribution_margin REAL NOT NULL DEFAULT 0,
    price_deviation REAL NOT NULL DEFAULT 0,
    strategy TEXT NOT NULL CHECK (strategy IN ('premium_positioning', 'market_average', 'competitive_pricing', 'margin_optimization', 'clearance')),
    justification TEXT NOT NULL DEFAULT '',
    data_quality TEXT NOT NULL DEFAULT '',
    risk_indicator TEXT NOT NULL DEFAULT '',
    is_approved INTEGER NOT NULL DEFAULT 0 CHECK (is_approved IN (0, 1)),
    applied_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_runs (
    id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    products_found INTEGER NOT NULL DEFAULT 0,
    matches_created INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error_message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pricing_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    rule_type TEXT NOT NULL CHECK (rule_type IN ('global', 'brand', 'category', 'product', 'sales_channel')),
    target_entity_id TEXT NOT NULL DEFAULT '',
    min_margin REAL NOT NULL DEFAULT 0,
    target_margin REAL,
    markup_percent REAL,
    min_selling_price REAL,
    max_selling_price REAL,
    strategy TEXT NOT NULL DEFAULT 'market_average',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- ── Google Merchant Center Tables ──
CREATE TABLE IF NOT EXISTS google_accounts (
    id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL UNIQUE,
    account_name TEXT NOT NULL,
    google_cloud_project_id TEXT NOT NULL,
    account_status TEXT NOT NULL DEFAULT 'pending' CHECK (account_status IN ('pending', 'active', 'suspended', 'disconnected')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    oauth_token_encrypted TEXT NOT NULL,
    oauth_refresh_token_encrypted TEXT,
    service_account_encrypted TEXT,
    scopes_json TEXT NOT NULL DEFAULT '[]',
    token_expires_at TEXT,
    last_token_validation TEXT,
    last_token_status TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS google_credentials (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    credential_type TEXT NOT NULL CHECK (credential_type IN ('oauth2', 'service_account')),
    credential_name TEXT NOT NULL,
    encrypted_value TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS google_data_sources (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    data_source_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'products',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'error')),
    primary_country TEXT NOT NULL DEFAULT 'DE',
    primary_language TEXT NOT NULL DEFAULT 'de',
    primary_currency TEXT NOT NULL DEFAULT 'EUR',
    feed_label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, data_source_id)
);
CREATE TABLE IF NOT EXISTS google_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    is_secret INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0, 1)),
    category TEXT NOT NULL DEFAULT 'general' CHECK (category IN ('general', 'export', 'sync', 'targeting', 'validation')),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS google_product_mappings (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    matterhorn_field TEXT NOT NULL,
    google_field TEXT NOT NULL,
    mapping_type TEXT NOT NULL CHECK (mapping_type IN ('direct', 'transform', 'fallback', 'conditional', 'concatenate', 'clean')),
    mapping_config_json TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS google_product_exports (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    product_sku TEXT NOT NULL,
    google_product_id TEXT NOT NULL DEFAULT '',
    offer_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'validation', 'exporting', 'completed', 'error', 'withdrawn', 'retry')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    response_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(product_sku, offer_id)
);
CREATE TABLE IF NOT EXISTS google_sync_jobs (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    job_type TEXT NOT NULL CHECK (job_type IN ('inventory', 'price', 'full_sync', 'product_update', 'withdrawal')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'error')),
    total_products INTEGER NOT NULL DEFAULT 0,
    processed_products INTEGER NOT NULL DEFAULT 0,
    failed_products INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    completed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS google_sync_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    product_sku TEXT NOT NULL,
    error_type TEXT NOT NULL,
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL DEFAULT '',
    resolved INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS google_product_diagnostics (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    product_sku TEXT NOT NULL,
    google_product_id TEXT NOT NULL DEFAULT '',
    diagnosis_status TEXT NOT NULL CHECK (diagnosis_status IN ('active', 'disapproved', 'pending', 'limited')),
    issues_json TEXT NOT NULL DEFAULT '[]',
    last_checked TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS google_performance_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    metric_date TEXT NOT NULL,
    impressions INTEGER NOT NULL DEFAULT 0,
    clicks INTEGER NOT NULL DEFAULT 0,
    ctr REAL NOT NULL DEFAULT 0,
    cpc REAL NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    conversions INTEGER NOT NULL DEFAULT 0,
    conversion_value REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS google_audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    target_type TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
-- ── OmniRoute Control Center Tables ──
CREATE TABLE IF NOT EXISTS ai_providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    provider_type TEXT NOT NULL CHECK (provider_type IN ('text', 'image', 'video', 'embedding', 'multimodal')),
    api_endpoint TEXT NOT NULL,
    api_key_encrypted TEXT NOT NULL,
    auth_type TEXT NOT NULL DEFAULT 'bearer' CHECK (auth_type IN ('bearer', 'api_key', 'oauth2')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    priority INTEGER NOT NULL DEFAULT 100,
    rate_limit_per_minute INTEGER NOT NULL DEFAULT 60,
    timeout_seconds INTEGER NOT NULL DEFAULT 30,
    max_retries INTEGER NOT NULL DEFAULT 3,
    retry_delay_seconds INTEGER NOT NULL DEFAULT 1,
    circuit_breaker_threshold INTEGER NOT NULL DEFAULT 5,
    circuit_breaker_timeout_seconds INTEGER NOT NULL DEFAULT 300,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    health_status TEXT NOT NULL DEFAULT 'unknown' CHECK (health_status IN ('healthy', 'degraded', 'unhealthy', 'unknown')),
    last_health_check TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_provider_credentials (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    credential_name TEXT NOT NULL,
    encrypted_value TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_models (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL DEFAULT '',
    model_category TEXT NOT NULL DEFAULT 'general' CHECK (model_category IN ('text', 'image', 'video', 'embedding', 'multimodal')),
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    context_window INTEGER NOT NULL DEFAULT 4096,
    input_price_per_1k REAL NOT NULL DEFAULT 0,
    output_price_per_1k REAL NOT NULL DEFAULT 0,
    image_price_per_image REAL NOT NULL DEFAULT 0,
    rate_limit_per_minute INTEGER NOT NULL DEFAULT 60,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    is_discoverable INTEGER NOT NULL DEFAULT 1 CHECK (is_discoverable IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider_id, model_id)
);
CREATE TABLE IF NOT EXISTS ai_model_capabilities (
    id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    capability_name TEXT NOT NULL,
    capability_value TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL DEFAULT 'discovered' CHECK (source IN ('discovered', 'manual')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_routing_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    strategy TEXT NOT NULL CHECK (strategy IN ('lowest_cost', 'highest_quality', 'lowest_latency', 'provider_priority', 'capability_matching', 'weighted_distribution')),
    provider_priority_json TEXT NOT NULL DEFAULT '[]',
    model_category TEXT NOT NULL DEFAULT 'general',
    min_quality_score REAL NOT NULL DEFAULT 0.0,
    max_cost_per_1k REAL,
    max_latency_ms INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_routing_policies (
    id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    fallback_rule_id TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workflow_name)
);
CREATE TABLE IF NOT EXISTS ai_workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    trigger_event TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    max_executions_per_hour INTEGER NOT NULL DEFAULT 1000,
    budget_per_execution REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_workflow_configs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    step_name TEXT NOT NULL,
    model_id TEXT,
    provider_id TEXT,
    prompt_template TEXT NOT NULL DEFAULT '',
    parameters_json TEXT NOT NULL DEFAULT '{}',
    step_order INTEGER NOT NULL DEFAULT 0,
    is_required INTEGER NOT NULL DEFAULT 1 CHECK (is_required IN (0, 1)),
    on_failure TEXT NOT NULL DEFAULT 'stop' CHECK (on_failure IN ('stop', 'continue', 'retry', 'fallback')),
    max_retries INTEGER NOT NULL DEFAULT 3,
    timeout_seconds INTEGER NOT NULL DEFAULT 30,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    model_id TEXT,
    workflow_id TEXT,
    request_type TEXT NOT NULL,
    tokens_input INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0,
    images_generated INTEGER NOT NULL DEFAULT 0,
    videos_generated INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'success' CHECK (status IN ('success', 'error', 'retry', 'fallback')),
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_cost_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    model_id TEXT,
    workflow_id TEXT,
    date TEXT NOT NULL,
    total_cost_usd REAL NOT NULL DEFAULT 0,
    request_count INTEGER NOT NULL DEFAULT 0,
    token_count INTEGER NOT NULL DEFAULT 0,
    image_count INTEGER NOT NULL DEFAULT 0,
    video_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_budgets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    budget_type TEXT NOT NULL CHECK (budget_type IN ('daily', 'monthly', 'provider', 'workflow')),
    target_id TEXT NOT NULL DEFAULT '',
    amount_usd REAL NOT NULL DEFAULT 0,
    warning_threshold_percent INTEGER NOT NULL DEFAULT 80,
    action_on_exceed TEXT NOT NULL DEFAULT 'warn' CHECK (action_on_exceed IN ('warn', 'pause', 'fallback', 'block')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    reset_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_provider_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('healthy', 'degraded', 'unhealthy', 'unknown')),
    response_time_ms INTEGER NOT NULL DEFAULT 0,
    error_rate REAL NOT NULL DEFAULT 0,
    last_check TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS ai_request_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    model_id TEXT,
    workflow_id TEXT,
    request_id TEXT NOT NULL,
    prompt_hash TEXT NOT NULL DEFAULT '',
    request_payload_json TEXT NOT NULL DEFAULT '{}',
    response_payload_json TEXT NOT NULL DEFAULT '{}',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'success',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_error_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT,
    model_id TEXT,
    workflow_id TEXT,
    error_type TEXT NOT NULL,
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'error' CHECK (severity IN ('debug', 'info', 'warning', 'error', 'critical')),
    resolved INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    resolved_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL DEFAULT 'system',
    action TEXT NOT NULL,
    target_type TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    asset_type TEXT NOT NULL CHECK (asset_type IN ('image', 'video', 'document', 'audio', 'graphic')),
    storage_backend TEXT NOT NULL DEFAULT 'local',
    storage_path TEXT NOT NULL,
    original_filename TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
    sha256_hash TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    duration_seconds REAL,
    source_type TEXT NOT NULL DEFAULT 'upload',
    source_url TEXT NOT NULL DEFAULT '',
    product_sku TEXT REFERENCES products(sku) ON DELETE SET NULL,
    model_profile_id TEXT,
    campaign_id TEXT,
    rights_status TEXT NOT NULL DEFAULT 'unknown' CHECK (rights_status IN ('unknown', 'owned', 'licensed', 'consent_granted', 'restricted')),
    approval_status TEXT NOT NULL DEFAULT 'draft' CHECK (approval_status IN ('draft', 'generating', 'generated', 'quality_check', 'review_required', 'approved', 'rejected', 'published', 'archived')),
    created_by TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_product_sku ON assets(product_sku);
CREATE INDEX IF NOT EXISTS idx_assets_model_profile_id ON assets(model_profile_id);
CREATE INDEX IF NOT EXISTS idx_assets_approval_status ON assets(approval_status);
CREATE INDEX IF NOT EXISTS idx_assets_sha256_hash ON assets(sha256_hash);
CREATE TABLE IF NOT EXISTS asset_versions (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    storage_path TEXT NOT NULL,
    sha256_hash TEXT NOT NULL,
    change_reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL,
    UNIQUE(asset_id, version_number)
);
CREATE INDEX IF NOT EXISTS idx_asset_versions_asset_id ON asset_versions(asset_id);
CREATE TABLE IF NOT EXISTS model_profiles (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    model_type TEXT NOT NULL CHECK (model_type IN ('own_identity', 'fictional', 'real_person')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    visual_traits_json TEXT NOT NULL DEFAULT '{}',
    pose_preferences_json TEXT NOT NULL DEFAULT '[]',
    style_description TEXT NOT NULL DEFAULT '',
    permitted_use_json TEXT NOT NULL DEFAULT '[]',
    consent_status TEXT NOT NULL DEFAULT 'not_required' CHECK (consent_status IN ('not_required', 'pending', 'granted', 'expired', 'revoked')),
    consent_valid_from TEXT,
    consent_valid_until TEXT,
    rights_holder TEXT NOT NULL DEFAULT '',
    approval_document_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_profiles_model_type ON model_profiles(model_type);
CREATE INDEX IF NOT EXISTS idx_model_profiles_consent_status ON model_profiles(consent_status);
CREATE TABLE IF NOT EXISTS model_references (
    id TEXT PRIMARY KEY,
    model_profile_id TEXT NOT NULL REFERENCES model_profiles(id) ON DELETE CASCADE,
    reference_type TEXT NOT NULL CHECK (reference_type IN ('front', 'profile', 'back', 'expression', 'full_body', 'pack')),
    asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL DEFAULT 1 CHECK (version_number > 0),
    quality_score REAL,
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_references_profile_id ON model_references(model_profile_id);
CREATE INDEX IF NOT EXISTS idx_model_references_asset_id ON model_references(asset_id);
CREATE TABLE IF NOT EXISTS model_permissions (
    id TEXT PRIMARY KEY,
    model_profile_id TEXT NOT NULL REFERENCES model_profiles(id) ON DELETE CASCADE,
    permission_type TEXT NOT NULL,
    granted INTEGER NOT NULL DEFAULT 0 CHECK (granted IN (0, 1)),
    scope_json TEXT NOT NULL DEFAULT '{}',
    valid_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(model_profile_id, permission_type)
);
CREATE INDEX IF NOT EXISTS idx_model_permissions_profile_id ON model_permissions(model_profile_id);
"""


async def get_pg_pool() -> Any:
    """Create a compact asyncpg pool, retrying while PostgreSQL starts."""
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    if asyncpg is None:
        raise DatabaseError("asyncpg is unavailable; install project requirements first.")
    if not uses_postgres():
        raise DatabaseError("DATABASE_URL must be postgresql://... for PostgreSQL mode.")

    for attempt in range(1, POSTGRES_RETRY_ATTEMPTS + 1):
        try:
            _pg_pool = await asyncpg.create_pool(
                dsn=DATABASE_URL,
                min_size=1,
                max_size=5,
                command_timeout=30,
                server_settings={"application_name": "omni-core"},
            )
            logger.info("PostgreSQL connection pool established on attempt %d.", attempt)
            return _pg_pool
        except Exception as exc:
            if attempt == POSTGRES_RETRY_ATTEMPTS:
                raise DatabaseError("PostgreSQL was not ready after %d attempts." % attempt) from exc
            logger.warning("PostgreSQL not ready (%d/%d): %s", attempt, POSTGRES_RETRY_ATTEMPTS, exc)
            await asyncio.sleep(POSTGRES_RETRY_DELAY_SECONDS)
    raise AssertionError("unreachable")


async def close_pg_pool() -> None:
    """Close the PostgreSQL pool if it has been created."""
    global _pg_pool
    if _pg_pool is not None:
        await _pg_pool.close()
        _pg_pool = None


async def _connect_sqlite() -> aiosqlite.Connection:
    """Open one configured SQLite WAL connection."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(DATABASE_PATH), timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    await conn.execute("PRAGMA foreign_keys=ON")
    return conn


@asynccontextmanager
async def get_connection() -> AsyncIterator[aiosqlite.Connection]:
    """Yield a SQLite connection for compatibility-only SQLite-specific callers."""
    if uses_postgres():
        raise DatabaseError("SQLite connection requested while PostgreSQL is configured.")
    conn = await _connect_sqlite()
    try:
        yield conn
    finally:
        await conn.close()


async def close_connection(conn: aiosqlite.Connection | None) -> None:
    """Close a SQLite connection; retained for backwards compatibility."""
    if conn is not None:
        await conn.close()


async def init_db() -> None:
    """Run automatic schema migrations for the selected storage backend."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                for statement in POSTGRES_SCHEMA:
                    await conn.execute(statement)
        logger.info("PostgreSQL schema migration complete.")
        return

    async with get_connection() as db:
        await db.executescript(SQLITE_SCHEMA)
        await db.commit()
    logger.info("SQLite WAL schema migration complete at %s.", DATABASE_PATH)


async def set_setting(key: str, value: str, *, is_secret: bool = False) -> None:
    """Idempotently store one setting."""
    if not key or not key.replace("_", "").isalnum():
        raise ValueError("Setting keys may only contain letters, digits, and underscores.")
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO settings(key, value, is_secret, updated_at) VALUES ($1, $2, $3, NOW())
                   ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value, is_secret = EXCLUDED.is_secret, updated_at = NOW()""",
                key, value, is_secret,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO settings(key, value, is_secret, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, is_secret = excluded.is_secret, updated_at = excluded.updated_at""",
            (key, value, int(is_secret), utc_now()),
        )
        await db.commit()


async def get_setting(key: str) -> str | None:
    """Get a raw setting for trusted server-side code only."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM settings WHERE key = $1", key)
            return str(row["value"]) if row else None
    async with get_connection() as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        await cursor.close()
        return str(row["value"]) if row else None


async def list_settings(*, reveal_secrets: bool = False) -> dict[str, str]:
    """List settings while masking secret values by default."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value, is_secret FROM settings ORDER BY key")
        return {str(r["key"]): str(r["value"]) if reveal_secrets or not r["is_secret"] else "********" for r in rows}
    async with get_connection() as db:
        cursor = await db.execute("SELECT key, value, is_secret FROM settings ORDER BY key")
        rows = await cursor.fetchall()
        await cursor.close()
        return {str(r["key"]): str(r["value"]) if reveal_secrets or not r["is_secret"] else "********" for r in rows}


async def write_log(level: str, event: str, message: str, context: Mapping[str, Any] | None = None) -> None:
    """Persist one bounded structured log event."""
    safe_level = level.upper()
    if safe_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        safe_level = "ERROR"
    payload = json.dumps(context or {}, ensure_ascii=False, separators=(",", ":"))[:4096]
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO system_logs(level, event, message, context_json) VALUES ($1, $2, $3, $4::jsonb)",
                safe_level, event[:128], message[:2000], payload,
            )
        return
    async with get_connection() as db:
        await db.execute(
            "INSERT INTO system_logs(level, event, message, context_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (safe_level, event[:128], message[:2000], payload, utc_now()),
        )
        await db.commit()


async def recent_logs(limit: int = 50) -> list[dict[str, Any]]:
    """Return recent bounded log records."""
    safe_limit = min(max(limit, 1), 200)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, level, event, message, context_json, created_at FROM system_logs ORDER BY id DESC LIMIT $1", safe_limit)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT id, level, event, message, context_json, created_at FROM system_logs ORDER BY id DESC LIMIT ?", (safe_limit,))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def upsert_product(*, sku: str, title: str, category: str, supplier_price: float, stock: int, source_hash: str, raw_description: str = "") -> bool:
    """Idempotently persist a supplier product and report if its source changed."""
    if not sku or supplier_price < 0 or stock < 0:
        raise ValueError("SKU must be non-empty; price and stock cannot be negative.")
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow("SELECT source_hash FROM products WHERE sku = $1", sku)
            if existing and existing["source_hash"] == source_hash:
                return False
            await conn.execute(
                """INSERT INTO products(sku, title, category, raw_description, supplier_price, stock, source_hash, updated_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                   ON CONFLICT(sku) DO UPDATE SET
                     title=EXCLUDED.title, category=EXCLUDED.category, raw_description=EXCLUDED.raw_description,
                     supplier_price=EXCLUDED.supplier_price, stock=EXCLUDED.stock, source_hash=EXCLUDED.source_hash,
                     status=CASE WHEN products.is_bundle THEN products.status ELSE 'active' END, updated_at=NOW()""",
                sku, title, category, raw_description, supplier_price, stock, source_hash,
            )
        return True
    async with get_connection() as db:
        cursor = await db.execute("SELECT source_hash FROM products WHERE sku = ?", (sku,))
        existing = await cursor.fetchone()
        await cursor.close()
        if existing and existing["source_hash"] == source_hash:
            return False
        await db.execute(
            """INSERT INTO products(sku, title, category, raw_description, supplier_price, stock, source_hash, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(sku) DO UPDATE SET
                 title=excluded.title, category=excluded.category, raw_description=excluded.raw_description,
                 supplier_price=excluded.supplier_price, stock=excluded.stock, source_hash=excluded.source_hash,
                 status=CASE WHEN products.is_bundle = 1 THEN products.status ELSE 'active' END, updated_at=excluded.updated_at""",
            (sku, title, category, raw_description, supplier_price, stock, source_hash, utc_now()),
        )
        await db.commit()
        return True


async def list_products(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Return products with bounded pagination."""
    safe_limit, safe_offset = min(max(limit, 1), 500), max(offset, 0)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM products ORDER BY updated_at DESC, sku ASC LIMIT $1 OFFSET $2", safe_limit, safe_offset)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM products ORDER BY updated_at DESC, sku ASC LIMIT ? OFFSET ?", (safe_limit, safe_offset))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def fetch_product(sku: str) -> dict[str, Any] | None:
    """Return one product by SKU."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM products WHERE sku = $1", sku)
        return dict(row) if row else None
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM products WHERE sku = ?", (sku,))
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None


async def list_bundles() -> list[dict[str, Any]]:
    """Return all bundle definitions."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM bundles ORDER BY bundle_sku")
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM bundles ORDER BY bundle_sku")
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def upsert_bundle(bundle_sku: str, title: str, component_skus: Sequence[str]) -> None:
    """Create/update a bundle definition; its stock is recalculated atomically."""
    components = [str(item) for item in component_skus if str(item)]
    if not components:
        raise ValueError("A bundle must contain at least one component.")
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO products(sku, title, category, supplier_price, stock, is_bundle, status, source_hash, updated_at)
                       VALUES ($1, $2, 'Bundle', 0, 0, TRUE, 'paused', $3, NOW())
                       ON CONFLICT(sku) DO UPDATE SET title=EXCLUDED.title, is_bundle=TRUE, updated_at=NOW()""",
                    bundle_sku, title, json.dumps(components, separators=(",", ":")),
                )
                await conn.execute(
                    """INSERT INTO bundles(bundle_sku, component_skus_json, title, updated_at) VALUES ($1, $2::jsonb, $3, NOW())
                       ON CONFLICT(bundle_sku) DO UPDATE SET component_skus_json=EXCLUDED.component_skus_json, title=EXCLUDED.title, updated_at=NOW()""",
                    bundle_sku, json.dumps(components), title,
                )
                await _refresh_bundle_stock_pg(conn, bundle_sku)
        return
    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                """INSERT INTO products(sku, title, category, supplier_price, stock, is_bundle, status, source_hash, updated_at)
                   VALUES (?, ?, 'Bundle', 0, 0, 1, 'paused', ?, ?)
                   ON CONFLICT(sku) DO UPDATE SET title=excluded.title, is_bundle=1, updated_at=excluded.updated_at""",
                (bundle_sku, title, json.dumps(components, separators=(",", ":")), utc_now()),
            )
            await db.execute(
                """INSERT INTO bundles(bundle_sku, component_skus_json, title, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(bundle_sku) DO UPDATE SET component_skus_json=excluded.component_skus_json, title=excluded.title, updated_at=excluded.updated_at""",
                (bundle_sku, json.dumps(components), title, utc_now()),
            )
            await _refresh_bundle_stock_sqlite(db, bundle_sku)
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def _refresh_bundle_stock_pg(conn: Any, bundle_sku: str) -> tuple[int, str]:
    bundle = await conn.fetchrow("SELECT component_skus_json FROM bundles WHERE bundle_sku = $1", bundle_sku)
    if not bundle:
        raise DatabaseError(f"Bundle {bundle_sku!r} does not exist.")
    components = [str(item) for item in bundle["component_skus_json"]]
    rows = await conn.fetch("SELECT sku, stock FROM products WHERE sku = ANY($1::text[])", components)
    stock_map = {str(row["sku"]): int(row["stock"]) for row in rows}
    stock = min((stock_map.get(component, 0) for component in components), default=0)
    status = "active" if stock > 0 else "paused"
    await conn.execute("UPDATE products SET stock=$1, status=$2, updated_at=NOW() WHERE sku=$3", stock, status, bundle_sku)
    return stock, status


async def _refresh_bundle_stock_sqlite(conn: aiosqlite.Connection, bundle_sku: str) -> tuple[int, str]:
    cursor = await conn.execute("SELECT component_skus_json FROM bundles WHERE bundle_sku = ?", (bundle_sku,))
    bundle = await cursor.fetchone()
    await cursor.close()
    if not bundle:
        raise DatabaseError(f"Bundle {bundle_sku!r} does not exist.")
    components = [str(item) for item in json.loads(str(bundle["component_skus_json"]))]
    placeholders = ",".join("?" for _ in components)
    cursor = await conn.execute(f"SELECT sku, stock FROM products WHERE sku IN ({placeholders})", tuple(components))
    rows = await cursor.fetchall()
    await cursor.close()
    stock_map = {str(row["sku"]): int(row["stock"]) for row in rows}
    stock = min((stock_map.get(component, 0) for component in components), default=0)
    status = "active" if stock > 0 else "paused"
    await conn.execute("UPDATE products SET stock=?, status=?, updated_at=? WHERE sku=?", (stock, status, utc_now(), bundle_sku))
    return stock, status


async def refresh_all_bundle_stocks() -> None:
    """Recalculate every bundle under one backend-specific write transaction."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch("SELECT bundle_sku FROM bundles")
                for row in rows:
                    await _refresh_bundle_stock_pg(conn, str(row["bundle_sku"]))
        return
    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("SELECT bundle_sku FROM bundles")
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                await _refresh_bundle_stock_sqlite(db, str(row["bundle_sku"]))
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def create_order(*, order_id: str, sku: str, quantity: int, marketplace: str, customer_reference: str = "") -> None:
    """Persist an idempotent incoming order in received state."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO orders(order_id, sku, quantity, marketplace, customer_reference, created_at, updated_at)
                   VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                   ON CONFLICT(order_id) DO UPDATE SET quantity=EXCLUDED.quantity, marketplace=EXCLUDED.marketplace,
                     customer_reference=EXCLUDED.customer_reference, updated_at=NOW()""",
                order_id, sku, quantity, marketplace, customer_reference,
            )
        return
    now = utc_now()
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO orders(order_id, sku, quantity, marketplace, customer_reference, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(order_id) DO UPDATE SET quantity=excluded.quantity, marketplace=excluded.marketplace,
                 customer_reference=excluded.customer_reference, updated_at=excluded.updated_at""",
            (order_id, sku, quantity, marketplace, customer_reference, now, now),
        )
        await db.commit()


async def reserve_order_stock(order_id: str, sku: str, quantity: int) -> None:
    """Atomically reserve product or all bundle component stock for an order."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow("SELECT status FROM orders WHERE order_id=$1 FOR UPDATE", order_id)
                if existing and existing["status"] in {"reserved", "dispatching", "dispatched"}:
                    return
                product = await conn.fetchrow("SELECT sku, stock, is_bundle, status FROM products WHERE sku=$1 FOR UPDATE", sku)
                if not product or product["status"] != "active" or int(product["stock"]) < quantity:
                    raise InsufficientStockError(f"Insufficient active stock for {sku}.")
                if product["is_bundle"]:
                    bundle = await conn.fetchrow("SELECT component_skus_json FROM bundles WHERE bundle_sku=$1", sku)
                    if not bundle:
                        raise DatabaseError(f"Bundle metadata missing for {sku}.")
                    for component in [str(item) for item in bundle["component_skus_json"]]:
                        changed = await conn.fetchval(
                            """UPDATE products SET stock=stock-$1, updated_at=NOW()
                               WHERE sku=$2 AND is_bundle=FALSE AND status='active' AND stock >= $1 RETURNING 1""",
                            quantity, component,
                        )
                        if changed != 1:
                            raise InsufficientStockError(f"Insufficient component stock for {component}.")
                else:
                    changed = await conn.fetchval(
                        "UPDATE products SET stock=stock-$1, updated_at=NOW() WHERE sku=$2 AND status='active' AND stock >= $1 RETURNING 1",
                        quantity, sku,
                    )
                    if changed != 1:
                        raise InsufficientStockError(f"Insufficient stock for {sku}.")
                for row in await conn.fetch("SELECT bundle_sku FROM bundles"):
                    await _refresh_bundle_stock_pg(conn, str(row["bundle_sku"]))
                await conn.execute("UPDATE orders SET status='reserved', last_error='', updated_at=NOW() WHERE order_id=$1", order_id)
        return

    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("SELECT status FROM orders WHERE order_id=?", (order_id,))
            existing = await cursor.fetchone()
            await cursor.close()
            if existing and existing["status"] in {"reserved", "dispatching", "dispatched"}:
                await db.commit()
                return
            cursor = await db.execute("SELECT sku, stock, is_bundle, status FROM products WHERE sku=?", (sku,))
            product = await cursor.fetchone()
            await cursor.close()
            if not product or product["status"] != "active" or int(product["stock"]) < quantity:
                raise InsufficientStockError(f"Insufficient active stock for {sku}.")
            components: list[str] = [sku]
            if product["is_bundle"]:
                cursor = await db.execute("SELECT component_skus_json FROM bundles WHERE bundle_sku=?", (sku,))
                bundle = await cursor.fetchone()
                await cursor.close()
                if not bundle:
                    raise DatabaseError(f"Bundle metadata missing for {sku}.")
                components = [str(item) for item in json.loads(str(bundle["component_skus_json"]))]
            for component in components:
                cursor = await db.execute(
                    """UPDATE products SET stock=stock-?, updated_at=?
                       WHERE sku=? AND status='active' AND stock >= ? AND (is_bundle=0 OR ?=?)""",
                    (quantity, utc_now(), component, quantity, component, sku),
                )
                changed = cursor.rowcount
                await cursor.close()
                if changed != 1:
                    raise InsufficientStockError(f"Insufficient component stock for {component}.")
            cursor = await db.execute("SELECT bundle_sku FROM bundles")
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                await _refresh_bundle_stock_sqlite(db, str(row["bundle_sku"]))
            await db.execute("UPDATE orders SET status='reserved', last_error='', updated_at=? WHERE order_id=?", (utc_now(), order_id))
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def update_order_status(order_id: str, status: str, *, tracking_number: str = "", tracking_url: str = "", error: str = "") -> None:
    """Update one order with fulfillment status, tracking data or error."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE orders SET status=$1, tracking_number=$2, tracking_url=$3, last_error=$4, updated_at=NOW() WHERE order_id=$5", status, tracking_number, tracking_url, error, order_id)
        return
    async with get_connection() as db:
        await db.execute("UPDATE orders SET status=?, tracking_number=?, tracking_url=?, last_error=?, updated_at=? WHERE order_id=?", (status, tracking_number, tracking_url, error, utc_now(), order_id))
        await db.commit()


async def list_orders(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Return orders with bounded pagination."""
    safe_limit, safe_offset = min(max(limit, 1), 500), max(offset, 0)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM orders ORDER BY created_at DESC, order_id ASC LIMIT $1 OFFSET $2", safe_limit, safe_offset)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM orders ORDER BY created_at DESC, order_id ASC LIMIT ? OFFSET ?", (safe_limit, safe_offset))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def count_dashboard_metrics() -> dict[str, int]:
    """Return compact dashboard counters."""
    query = """SELECT
        (SELECT COUNT(*) FROM products) AS product_count,
        (SELECT COUNT(*) FROM products WHERE status='active') AS active_product_count,
        (SELECT COUNT(*) FROM orders WHERE status IN ('received', 'reserved', 'dispatching')) AS pending_order_count,
        (SELECT COUNT(*) FROM system_logs WHERE level='ERROR' AND created_at >= %s) AS error_count"""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query % "NOW() - INTERVAL '24 hours'")
        return {key: int(value) for key, value in dict(row).items()} if row else {}
    async with get_connection() as db:
        cursor = await db.execute(query % "datetime('now', '-24 hours')")
        row = await cursor.fetchone()
        await cursor.close()
        return {key: int(row[key]) for key in row.keys()} if row else {}


async def start_import_run(source: str) -> int:
    """Create an import-run record and return its id."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            return int(await conn.fetchval("INSERT INTO import_runs(source) VALUES ($1) RETURNING id", source))
    async with get_connection() as db:
        cursor = await db.execute("INSERT INTO import_runs(source, started_at) VALUES (?, ?)", (source, utc_now()))
        await db.commit()
        run_id = int(cursor.lastrowid or 0)
        await cursor.close()
        return run_id


async def finish_import_run(run_id: int, *, imported: int, updated: int, errors: int, error_message: str = "") -> None:
    """Complete an import-run record."""
    status = "success" if errors == 0 else "failed"
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE import_runs SET finished_at=NOW(), imported=$1, updated=$2, errors=$3, status=$4, error_message=$5 WHERE id=$6", imported, updated, errors, status, error_message[:2000], run_id)
        return
    async with get_connection() as db:
        await db.execute("UPDATE import_runs SET finished_at=?, imported=?, updated=?, errors=?, status=?, error_message=? WHERE id=?", (utc_now(), imported, updated, errors, status, error_message[:2000], run_id))
        await db.commit()


async def recent_import_runs(limit: int = 10) -> list[dict[str, Any]]:
    """Return recent import-run records."""
    safe_limit = min(max(limit, 1), 50)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM import_runs ORDER BY id DESC LIMIT $1", safe_limit)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM import_runs ORDER BY id DESC LIMIT ?", (safe_limit,))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


# ════════════════════════════════════════════════════════════════════════════
# Google Merchant Center - Account Management
# ════════════════════════════════════════════════════════════════════════════

async def google_upsert_account(*, account_id: str, merchant_id: str, account_name: str,
                                 google_cloud_project_id: str, oauth_token_encrypted: str,
                                 oauth_refresh_token_encrypted: str | None = None,
                                 service_account_encrypted: str | None = None,
                                 is_primary: bool = False) -> None:
    """Upsert a Google Merchant Center account."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO google_accounts(id, merchant_id, account_name, google_cloud_project_id,
                   oauth_token_encrypted, oauth_refresh_token_encrypted, service_account_encrypted,
                   is_primary, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW())
                   ON CONFLICT(id) DO UPDATE SET
                     merchant_id=EXCLUDED.merchant_id, account_name=EXCLUDED.account_name,
                     google_cloud_project_id=EXCLUDED.google_cloud_project_id,
                     oauth_token_encrypted=EXCLUDED.oauth_token_encrypted,
                     oauth_refresh_token_encrypted=EXCLUDED.oauth_refresh_token_encrypted,
                     service_account_encrypted=EXCLUDED.service_account_encrypted,
                     is_primary=EXCLUDED.is_primary, updated_at=NOW()""",
                account_id, merchant_id, account_name, google_cloud_project_id,
                oauth_token_encrypted, oauth_refresh_token_encrypted, service_account_encrypted, is_primary,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO google_accounts(id, merchant_id, account_name, google_cloud_project_id,
               oauth_token_encrypted, oauth_refresh_token_encrypted, service_account_encrypted,
               is_active, is_primary, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,1,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 merchant_id=excluded.merchant_id, account_name=excluded.account_name,
                 google_cloud_project_id=excluded.google_cloud_project_id,
                 oauth_token_encrypted=excluded.oauth_token_encrypted,
                 oauth_refresh_token_encrypted=excluded.oauth_refresh_token_encrypted,
                 service_account_encrypted=excluded.service_account_encrypted,
                 is_primary=excluded.is_primary, updated_at=excluded.updated_at""",
            (account_id, merchant_id, account_name, google_cloud_project_id,
             oauth_token_encrypted, oauth_refresh_token_encrypted, service_account_encrypted,
             is_primary, utc_now(), utc_now()),
        )
        await db.commit()


async def google_list_accounts() -> list[dict[str, Any]]:
    """List all Google Merchant Center accounts."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM google_accounts ORDER BY account_name")
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM google_accounts ORDER BY account_name")
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def google_get_account(account_id: str) -> dict[str, Any] | None:
    """Get a single Google account by ID."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM google_accounts WHERE id = $1", account_id)
        return dict(row) if row else None
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM google_accounts WHERE id = ?", (account_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None


# ════════════════════════════════════════════════════════════════════════════
# Google Merchant Center - Data Sources
# ════════════════════════════════════════════════════════════════════════════

async def google_upsert_data_source(*, ds_id: str, account_id: str, data_source_id: str,
                                     display_name: str, primary_country: str = "DE",
                                     primary_language: str = "de", primary_currency: str = "EUR") -> None:
    """Upsert a Google data source."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO google_data_sources(id, account_id, data_source_id, display_name,
                   primary_country, primary_language, primary_currency, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,NOW())
                   ON CONFLICT(id) DO UPDATE SET
                     display_name=EXCLUDED.display_name, primary_country=EXCLUDED.primary_country,
                     primary_language=EXCLUDED.primary_language, primary_currency=EXCLUDED.primary_currency,
                     updated_at=NOW()""",
                ds_id, account_id, data_source_id, display_name, primary_country,
                primary_language, primary_currency,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO google_data_sources(id, account_id, data_source_id, display_name,
               source_type, status, primary_country, primary_language, primary_currency,
               feed_label, created_at, updated_at)
               VALUES (?,?,?,?,'products','active',?,?,?, '', ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 display_name=excluded.display_name, primary_country=excluded.primary_country,
                 primary_language=excluded.primary_language, primary_currency=excluded.primary_currency,
                 updated_at=excluded.updated_at""",
            (ds_id, account_id, data_source_id, display_name, primary_country,
             primary_language, primary_currency, utc_now(), utc_now()),
        )
        await db.commit()


async def google_list_data_sources(account_id: str | None = None) -> list[dict[str, Any]]:
    """List data sources, optionally filtered by account."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if account_id:
                rows = await conn.fetch("SELECT * FROM google_data_sources WHERE account_id = $1 ORDER BY display_name", account_id)
            else:
                rows = await conn.fetch("SELECT * FROM google_data_sources ORDER BY display_name")
        return [dict(row) for row in rows]
    async with get_connection() as db:
        if account_id:
            cursor = await db.execute("SELECT * FROM google_data_sources WHERE account_id = ? ORDER BY display_name", (account_id,))
        else:
            cursor = await db.execute("SELECT * FROM google_data_sources ORDER BY display_name")
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


# ════════════════════════════════════════════════════════════════════════════
# Google Merchant Center - Product Exports
# ════════════════════════════════════════════════════════════════════════════

async def google_upsert_export(*, export_id: str, account_id: str, product_sku: str,
                                offer_id: str, status: str = "pending",
                                payload_json: str = "{}", response_json: str = "{}",
                                error_message: str = "", max_retries: int = 3) -> None:
    """Upsert a Google product export record."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO google_product_exports(id, account_id, product_sku, offer_id,
                   status, payload_json, response_json, error_message, max_retries, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9,NOW())
                   ON CONFLICT(product_sku, offer_id) DO UPDATE SET
                     status=EXCLUDED.status, payload_json=EXCLUDED.payload_json,
                     response_json=EXCLUDED.response_json, error_message=EXCLUDED.error_message,
                     max_retries=EXCLUDED.max_retries, updated_at=NOW()""",
                export_id, account_id, product_sku, offer_id, status,
                payload_json, response_json, error_message, max_retries,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO google_product_exports(id, account_id, product_sku, google_product_id,
               offer_id, status, payload_json, response_json, error_message,
               max_retries, created_at, updated_at)
               VALUES (?,?,?,'',?,?,?,?,?,?,?,?)
               ON CONFLICT(product_sku, offer_id) DO UPDATE SET
                 status=excluded.status, payload_json=excluded.payload_json,
                 response_json=excluded.response_json, error_message=excluded.error_message,
                 max_retries=excluded.max_retries, updated_at=excluded.updated_at""",
            (export_id, account_id, product_sku, offer_id, status, payload_json,
             response_json, error_message, max_retries, utc_now(), utc_now()),
        )
        await db.commit()


async def google_list_exports(account_id: str | None = None, status: str | None = None,
                                limit: int = 100) -> list[dict[str, Any]]:
    """List Google product exports with optional filtering."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            query = "SELECT * FROM google_product_exports WHERE 1=1"
            params: list[Any] = []
            if account_id:
                query += f" AND account_id = ${len(params) + 1}"
                params.append(account_id)
            if status:
                query += f" AND status = ${len(params) + 1}"
                params.append(status)
            query += f" ORDER BY created_at DESC LIMIT ${len(params) + 1}"
            params.append(limit)
            rows = await conn.fetch(query, *params)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        query = "SELECT * FROM google_product_exports WHERE 1=1"
        params: list[Any] = []
        if account_id:
            query += " AND account_id = ?"
            params.append(account_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor = await db.execute(query, tuple(params))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


# ════════════════════════════════════════════════════════════════════════════
# Google Merchant Center - Sync Jobs
# ════════════════════════════════════════════════════════════════════════════

async def google_upsert_sync_job(*, job_id: str, account_id: str, job_type: str,
                                  total_products: int = 0) -> None:
    """Upsert a Google sync job record."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO google_sync_jobs(id, account_id, job_type, total_products, started_at)
                   VALUES ($1,$2,$3,$4,NOW())
                   ON CONFLICT(id) DO UPDATE SET
                     total_products=EXCLUDED.total_products, updated_at=NOW()""",
                job_id, account_id, job_type, total_products,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO google_sync_jobs(id, account_id, job_type, status, total_products,
               processed_products, failed_products, started_at, created_at)
               VALUES (?,?,'pending',?,0,0,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 total_products=excluded.total_products, updated_at=excluded.updated_at""",
            (job_id, account_id, job_type, total_products, utc_now(), utc_now()),
        )
        await db.commit()


async def google_list_sync_jobs(account_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """List Google sync jobs."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if account_id:
                rows = await conn.fetch("SELECT * FROM google_sync_jobs WHERE account_id = $1 ORDER BY created_at DESC LIMIT $2", account_id, limit)
            else:
                rows = await conn.fetch("SELECT * FROM google_sync_jobs ORDER BY created_at DESC LIMIT $1", limit)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        if account_id:
            cursor = await db.execute("SELECT * FROM google_sync_jobs WHERE account_id = ? ORDER BY created_at DESC LIMIT ?", (account_id, limit))
        else:
            cursor = await db.execute("SELECT * FROM google_sync_jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


# ════════════════════════════════════════════════════════════════════════════
# OmniRoute - Provider Management
# ════════════════════════════════════════════════════════════════════════════

async def ai_upsert_provider(*, provider_id: str, name: str, provider_type: str,
                              api_endpoint: str, api_key_encrypted: str,
                              auth_type: str = "bearer", priority: int = 100,
                              rate_limit_per_minute: int = 60, timeout_seconds: int = 30,
                              max_retries: int = 3, retry_delay_seconds: int = 1,
                              circuit_breaker_threshold: int = 5,
                              circuit_breaker_timeout_seconds: int = 300,
                              capabilities_json: str = "[]") -> None:
    """Upsert an AI provider."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO ai_providers(id, name, provider_type, api_endpoint,
                   api_key_encrypted, auth_type, priority, rate_limit_per_minute,
                   timeout_seconds, max_retries, retry_delay_seconds,
                   circuit_breaker_threshold, circuit_breaker_timeout_seconds,
                   capabilities_json, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,NOW())
                   ON CONFLICT(id) DO UPDATE SET
                     name=EXCLUDED.name, provider_type=EXCLUDED.provider_type,
                     api_endpoint=EXCLUDED.api_endpoint, api_key_encrypted=EXCLUDED.api_key_encrypted,
                     auth_type=EXCLUDED.auth_type, priority=EXCLUDED.priority,
                     rate_limit_per_minute=EXCLUDED.rate_limit_per_minute,
                     timeout_seconds=EXCLUDED.timeout_seconds, max_retries=EXCLUDED.max_retries,
                     retry_delay_seconds=EXCLUDED.retry_delay_seconds,
                     circuit_breaker_threshold=EXCLUDED.circuit_breaker_threshold,
                     circuit_breaker_timeout_seconds=EXCLUDED.circuit_breaker_timeout_seconds,
                     capabilities_json=EXCLUDED.capabilities_json, updated_at=NOW()""",
                provider_id, name, provider_type, api_endpoint, api_key_encrypted,
                auth_type, priority, rate_limit_per_minute, timeout_seconds, max_retries,
                retry_delay_seconds, circuit_breaker_threshold, circuit_breaker_timeout_seconds,
                capabilities_json,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO ai_providers(id, name, provider_type, api_endpoint,
               api_key_encrypted, auth_type, is_active, priority, rate_limit_per_minute,
               timeout_seconds, max_retries, retry_delay_seconds,
               circuit_breaker_threshold, circuit_breaker_timeout_seconds,
               capabilities_json, health_status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,'unknown',?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, provider_type=excluded.provider_type,
                 api_endpoint=excluded.api_endpoint, api_key_encrypted=excluded.api_key_encrypted,
                 auth_type=excluded.auth_type, priority=excluded.priority,
                 rate_limit_per_minute=excluded.rate_limit_per_minute,
                 timeout_seconds=excluded.timeout_seconds, max_retries=excluded.max_retries,
                 retry_delay_seconds=excluded.retry_delay_seconds,
                 circuit_breaker_threshold=excluded.circuit_breaker_threshold,
                 circuit_breaker_timeout_seconds=excluded.circuit_breaker_timeout_seconds,
                 capabilities_json=excluded.capabilities_json, updated_at=excluded.updated_at""",
            (provider_id, name, provider_type, api_endpoint, api_key_encrypted, auth_type,
             priority, rate_limit_per_minute, timeout_seconds, max_retries,
             retry_delay_seconds, circuit_breaker_threshold, circuit_breaker_timeout_seconds,
             capabilities_json, utc_now(), utc_now()),
        )
        await db.commit()


async def ai_list_providers() -> list[dict[str, Any]]:
    """List all AI providers."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM ai_providers ORDER BY priority, name")
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM ai_providers ORDER BY priority, name")
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


# ════════════════════════════════════════════════════════════════════════════
# OmniRoute - Model Management
# ════════════════════════════════════════════════════════════════════════════

async def ai_upsert_model(*, model_id: str, provider_id: str, model_id_str: str,
                           model_name: str, model_category: str = "general",
                           context_window: int = 4096,
                           input_price_per_1k: float = 0.0,
                           output_price_per_1k: float = 0.0,
                           image_price_per_image: float = 0.0,
                           rate_limit_per_minute: int = 60,
                           capabilities_json: str = "[]",
                           is_active: bool = True) -> None:
    """Upsert an AI model."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO ai_models(id, provider_id, model_id, model_name,
                   model_category, context_window, input_price_per_1k,
                   output_price_per_1k, image_price_per_image, rate_limit_per_minute,
                   capabilities_json, is_active, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12,NOW())
                   ON CONFLICT(provider_id, model_id) DO UPDATE SET
                     model_name=EXCLUDED.model_name, model_category=EXCLUDED.model_category,
                     context_window=EXCLUDED.context_window,
                     input_price_per_1k=EXCLUDED.input_price_per_1k,
                     output_price_per_1k=EXCLUDED.output_price_per_1k,
                     image_price_per_image=EXCLUDED.image_price_per_image,
                     rate_limit_per_minute=EXCLUDED.rate_limit_per_minute,
                     capabilities_json=EXCLUDED.capabilities_json,
                     is_active=EXCLUDED.is_active, updated_at=NOW()""",
                model_id, provider_id, model_id_str, model_name, model_category,
                context_window, input_price_per_1k, output_price_per_1k,
                image_price_per_image, rate_limit_per_minute, capabilities_json,
                is_active,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO ai_models(id, provider_id, model_id, model_name,
               model_category, context_window, input_price_per_1k,
               output_price_per_1k, image_price_per_image, rate_limit_per_minute,
               capabilities_json, is_active, is_discoverable, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1,1,?,?)
               ON CONFLICT(provider_id, model_id) DO UPDATE SET
                 model_name=excluded.model_name, model_category=excluded.model_category,
                 context_window=excluded.context_window,
                 input_price_per_1k=excluded.input_price_per_1k,
                 output_price_per_1k=excluded.output_price_per_1k,
                 image_price_per_image=excluded.image_price_per_image,
                 rate_limit_per_minute=excluded.rate_limit_per_minute,
                 capabilities_json=excluded.capabilities_json,
                 is_active=excluded.is_active, updated_at=excluded.updated_at""",
            (model_id, provider_id, model_id_str, model_name, model_category,
             context_window, input_price_per_1k, output_price_per_1k,
             image_price_per_image, rate_limit_per_minute, capabilities_json,
             is_active, utc_now(), utc_now()),
        )
        await db.commit()


async def ai_list_models(provider_id: str | None = None) -> list[dict[str, Any]]:
    """List AI models, optionally filtered by provider."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if provider_id:
                rows = await conn.fetch("SELECT * FROM ai_models WHERE provider_id = $1 ORDER BY model_name", provider_id)
            else:
                rows = await conn.fetch("SELECT * FROM ai_models ORDER BY model_name")
        return [dict(row) for row in rows]
    async with get_connection() as db:
        if provider_id:
            cursor = await db.execute("SELECT * FROM ai_models WHERE provider_id = ? ORDER BY model_name", (provider_id,))
        else:
            cursor = await db.execute("SELECT * FROM ai_models ORDER BY model_name")
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


# ════════════════════════════════════════════════════════════════════════════
# OmniRoute - Usage Records
# ════════════════════════════════════════════════════════════════════════════

async def ai_insert_usage_record(*, record_id: str, provider_id: str,
                                  model_id: str | None, workflow_id: str | None,
                                  request_type: str, tokens_input: int = 0,
                                  tokens_output: int = 0, images_generated: int = 0,
                                  videos_generated: int = 0, cost_usd: float = 0.0,
                                  latency_ms: int = 0, status: str = "success",
                                  error_message: str = "") -> None:
    """Insert an AI usage record."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO ai_usage_records(id, provider_id, model_id, workflow_id,
                   request_type, tokens_input, tokens_output, images_generated,
                   videos_generated, cost_usd, latency_ms, status, error_message)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
                record_id, provider_id, model_id, workflow_id, request_type,
                tokens_input, tokens_output, images_generated, videos_generated,
                cost_usd, latency_ms, status, error_message,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO ai_usage_records(id, provider_id, model_id, workflow_id,
               request_type, tokens_input, tokens_output, images_generated,
               videos_generated, cost_usd, latency_ms, status, error_message, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record_id, provider_id, model_id, workflow_id, request_type,
             tokens_input, tokens_output, images_generated, videos_generated,
             cost_usd, latency_ms, status, error_message, utc_now()),
        )
        await db.commit()


async def ai_list_usage_records(limit: int = 100) -> list[dict[str, Any]]:
    """List recent AI usage records."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM ai_usage_records ORDER BY created_at DESC LIMIT $1", limit)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM ai_usage_records ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


# ════════════════════════════════════════════════════════════════════════════
# Asset Management
# ════════════════════════════════════════════════════════════════════════════

async def asset_find_by_hash(sha256_hash: str) -> dict[str, Any] | None:
    """Return an existing asset with the same SHA-256 hash."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM assets WHERE sha256_hash = $1 ORDER BY created_at ASC LIMIT 1", sha256_hash)
        return dict(row) if row else None
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM assets WHERE sha256_hash = ? ORDER BY created_at ASC LIMIT 1", (sha256_hash,))
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None


async def asset_create(*, asset_id: str, asset_type: str, storage_path: str,
                       original_filename: str = "", mime_type: str = "",
                       size_bytes: int = 0, sha256_hash: str = "",
                       width: int | None = None, height: int | None = None,
                       duration_seconds: float | None = None,
                       source_type: str = "upload", source_url: str = "",
                       product_sku: str | None = None,
                       model_profile_id: str | None = None,
                       campaign_id: str | None = None,
                       rights_status: str = "unknown",
                       approval_status: str = "draft",
                       created_by: str = "system") -> None:
    """Persist a new asset record."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO assets(id, asset_type, storage_path, original_filename, mime_type,
                   size_bytes, sha256_hash, width, height, duration_seconds, source_type, source_url,
                   product_sku, model_profile_id, campaign_id, rights_status, approval_status,
                   created_by, created_at, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,NOW(),NOW())""",
                asset_id, asset_type, storage_path, original_filename, mime_type, size_bytes,
                sha256_hash, width, height, duration_seconds, source_type, source_url, product_sku,
                model_profile_id, campaign_id, rights_status, approval_status, created_by,
            )
        return
    now = utc_now()
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO assets(id, asset_type, storage_backend, storage_path, original_filename,
               mime_type, size_bytes, sha256_hash, width, height, duration_seconds, source_type,
               source_url, product_sku, model_profile_id, campaign_id, rights_status,
               approval_status, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (asset_id, asset_type, "local", storage_path, original_filename, mime_type, size_bytes,
             sha256_hash, width, height, duration_seconds, source_type, source_url, product_sku,
             model_profile_id, campaign_id, rights_status, approval_status, created_by, now, now),
        )
        await db.commit()


async def asset_get(asset_id: str) -> dict[str, Any] | None:
    """Return one asset by ID."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM assets WHERE id = $1", asset_id)
        return dict(row) if row else None
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM assets WHERE id = ?", (asset_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None


async def asset_list(*, product_sku: str | None = None, model_profile_id: str | None = None,
                     asset_type: str | None = None, approval_status: str | None = None,
                     limit: int = 100) -> list[dict[str, Any]]:
    """Return assets with optional filters."""
    safe_limit = min(max(limit, 1), 500)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            query = "SELECT * FROM assets WHERE 1=1"
            params: list[Any] = []
            if product_sku:
                query += f" AND product_sku = ${len(params) + 1}"
                params.append(product_sku)
            if model_profile_id:
                query += f" AND model_profile_id = ${len(params) + 1}"
                params.append(model_profile_id)
            if asset_type:
                query += f" AND asset_type = ${len(params) + 1}"
                params.append(asset_type)
            if approval_status:
                query += f" AND approval_status = ${len(params) + 1}"
                params.append(approval_status)
            query += f" ORDER BY created_at DESC, id ASC LIMIT ${len(params) + 1}"
            params.append(safe_limit)
            rows = await conn.fetch(query, *params)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        query = "SELECT * FROM assets WHERE 1=1"
        params: list[Any] = []
        if product_sku:
            query += " AND product_sku = ?"
            params.append(product_sku)
        if model_profile_id:
            query += " AND model_profile_id = ?"
            params.append(model_profile_id)
        if asset_type:
            query += " AND asset_type = ?"
            params.append(asset_type)
        if approval_status:
            query += " AND approval_status = ?"
            params.append(approval_status)
        query += " ORDER BY created_at DESC, id ASC LIMIT ?"
        params.append(safe_limit)
        cursor = await db.execute(query, tuple(params))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def asset_update(asset_id: str, **fields: Any) -> bool:
    """Update mutable asset fields and return whether a row was changed."""
    allowed = {
        "asset_type", "storage_path", "original_filename", "mime_type", "size_bytes",
        "width", "height", "duration_seconds", "source_type", "source_url", "product_sku",
        "model_profile_id", "campaign_id", "rights_status", "approval_status",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return False
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            assignments = []
            params: list[Any] = []
            for index, (key, value) in enumerate(updates.items(), start=1):
                assignments.append(f"{key} = ${index}")
                params.append(value)
            params.append(asset_id)
            result = await conn.execute(
                f"UPDATE assets SET {', '.join(assignments)}, updated_at = NOW() WHERE id = ${len(params)}",
                *params,
            )
        return result.endswith("1")
    async with get_connection() as db:
        assignments = [f"{key} = ?" for key in updates]
        params = list(updates.values()) + [asset_id]
        cursor = await db.execute(
            f"UPDATE assets SET {', '.join(assignments)}, updated_at = ? WHERE id = ?",
            tuple(params[:-1] + [utc_now(), params[-1]]),
        )
        await db.commit()
        changed = cursor.rowcount > 0
        await cursor.close()
        return changed


async def asset_version_create(*, version_id: str, asset_id: str, version_number: int,
                               storage_path: str, sha256_hash: str,
                               change_reason: str = "", created_by: str = "system") -> None:
    """Persist an immutable asset version."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO asset_versions(id, asset_id, version_number, storage_path,
                   sha256_hash, change_reason, created_by, created_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,NOW())""",
                version_id, asset_id, version_number, storage_path, sha256_hash,
                change_reason, created_by,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO asset_versions(id, asset_id, version_number, storage_path,
               sha256_hash, change_reason, created_by, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (version_id, asset_id, version_number, storage_path, sha256_hash,
             change_reason, created_by, utc_now()),
        )
        await db.commit()


async def asset_version_list(asset_id: str) -> list[dict[str, Any]]:
    """Return versions for one asset in ascending order."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM asset_versions WHERE asset_id = $1 ORDER BY version_number ASC", asset_id)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM asset_versions WHERE asset_id = ? ORDER BY version_number ASC", (asset_id,))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


# ════════════════════════════════════════════════════════════════════════════
# Model Identity Management
# ════════════════════════════════════════════════════════════════════════════

async def model_profile_create(*, profile_id: str, display_name: str, model_type: str,
                               active: bool = True, visual_traits: dict[str, Any] | None = None,
                               pose_preferences: list[str] | None = None,
                               style_description: str = "",
                               permitted_use: list[str] | None = None,
                               consent_status: str = "not_required",
                               consent_valid_from: str | None = None,
                               consent_valid_until: str | None = None,
                               rights_holder: str = "",
                               approval_document_ref: str = "") -> None:
    """Persist a model identity profile."""
    traits = visual_traits or {}
    poses = pose_preferences or []
    uses = permitted_use or []
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO model_profiles(id, display_name, model_type, active,
                   visual_traits_json, pose_preferences_json, style_description,
                   permitted_use_json, consent_status, consent_valid_from,
                   consent_valid_until, rights_holder, approval_document_ref,
                   created_at, updated_at)
                   VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8::jsonb,$9,$10,$11,$12,$13,NOW(),NOW())""",
                profile_id, display_name, model_type, active,
                __import__("json").dumps(traits), __import__("json").dumps(poses),
                style_description, __import__("json").dumps(uses), consent_status,
                consent_valid_from, consent_valid_until, rights_holder, approval_document_ref,
            )
        return
    now = utc_now()
    import json as _json
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO model_profiles(id, display_name, model_type, active,
               visual_traits_json, pose_preferences_json, style_description,
               permitted_use_json, consent_status, consent_valid_from,
               consent_valid_until, rights_holder, approval_document_ref,
               created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (profile_id, display_name, model_type, int(active),
             _json.dumps(traits), _json.dumps(poses), style_description,
             _json.dumps(uses), consent_status, consent_valid_from,
             consent_valid_until, rights_holder, approval_document_ref, now, now),
        )
        await db.commit()


async def model_profile_list(*, model_type: str | None = None,
                             consent_status: str | None = None,
                             limit: int = 100) -> list[dict[str, Any]]:
    """List model profiles with optional filters."""
    safe_limit = min(max(limit, 1), 500)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            query = "SELECT * FROM model_profiles WHERE 1=1"
            params: list[Any] = []
            if model_type:
                query += f" AND model_type = ${len(params) + 1}"
                params.append(model_type)
            if consent_status:
                query += f" AND consent_status = ${len(params) + 1}"
                params.append(consent_status)
            query += f" ORDER BY display_name ASC, id ASC LIMIT ${len(params) + 1}"
            params.append(safe_limit)
            rows = await conn.fetch(query, *params)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        query = "SELECT * FROM model_profiles WHERE 1=1"
        params: list[Any] = []
        if model_type:
            query += " AND model_type = ?"
            params.append(model_type)
        if consent_status:
            query += " AND consent_status = ?"
            params.append(consent_status)
        query += " ORDER BY display_name ASC, id ASC LIMIT ?"
        params.append(safe_limit)
        cursor = await db.execute(query, tuple(params))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def model_profile_get(profile_id: str) -> dict[str, Any] | None:
    """Return one model profile by ID."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM model_profiles WHERE id = $1", profile_id)
        return dict(row) if row else None
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM model_profiles WHERE id = ?", (profile_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None


async def model_profile_update(profile_id: str, **fields: Any) -> bool:
    """Update mutable model profile fields."""
    import json as _json
    allowed = {
        "display_name", "model_type", "active", "visual_traits", "pose_preferences",
        "style_description", "permitted_use", "consent_status", "consent_valid_from",
        "consent_valid_until", "rights_holder", "approval_document_ref",
    }
    column_map = {
        "visual_traits": "visual_traits_json",
        "pose_preferences": "pose_preferences_json",
        "permitted_use": "permitted_use_json",
    }
    updates = {}
    for key, value in fields.items():
        if key not in allowed:
            continue
        column = column_map.get(key, key)
        if key in {"visual_traits", "pose_preferences", "permitted_use"}:
            updates[column] = _json.dumps(value if value is not None else ({} if key == "visual_traits" else []))
        elif key == "active":
            updates[column] = bool(value)
        else:
            updates[column] = value
    if not updates:
        return False
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            assignments = []
            params: list[Any] = []
            for index, (column, value) in enumerate(updates.items(), start=1):
                cast = "::jsonb" if column.endswith("_json") else ""
                assignments.append(f"{column} = ${index}{cast}")
                params.append(value)
            params.append(profile_id)
            result = await conn.execute(
                f"UPDATE model_profiles SET {', '.join(assignments)}, updated_at = NOW() WHERE id = ${len(params)}",
                *params,
            )
        return result.endswith("1")
    async with get_connection() as db:
        assignments = [f"{column} = ?" for column in updates]
        params = list(updates.values()) + [utc_now(), profile_id]
        cursor = await db.execute(
            f"UPDATE model_profiles SET {', '.join(assignments)}, updated_at = ? WHERE id = ?",
            tuple(params),
        )
        await db.commit()
        changed = cursor.rowcount > 0
        await cursor.close()
        return changed


async def model_reference_create(*, reference_id: str, model_profile_id: str,
                                 reference_type: str, asset_id: str,
                                 version_number: int = 1,
                                 quality_score: float | None = None,
                                 is_primary: bool = False) -> None:
    """Persist a model reference image."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO model_references(id, model_profile_id, reference_type, asset_id,
                   version_number, quality_score, is_primary, created_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,NOW())""",
                reference_id, model_profile_id, reference_type, asset_id, version_number,
                quality_score, is_primary,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO model_references(id, model_profile_id, reference_type, asset_id,
               version_number, quality_score, is_primary, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (reference_id, model_profile_id, reference_type, asset_id, version_number,
             quality_score, int(is_primary), utc_now()),
        )
        await db.commit()


async def model_reference_list(model_profile_id: str) -> list[dict[str, Any]]:
    """Return references for one model profile."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM model_references WHERE model_profile_id = $1 ORDER BY is_primary DESC, created_at ASC", model_profile_id)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM model_references WHERE model_profile_id = ? ORDER BY is_primary DESC, created_at ASC", (model_profile_id,))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def model_permission_upsert(*, permission_id: str, model_profile_id: str,
                                  permission_type: str, granted: bool,
                                  scope: dict[str, Any] | None = None,
                                  valid_until: str | None = None) -> None:
    """Upsert a model usage permission."""
    import json as _json
    scope_json = _json.dumps(scope or {})
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO model_permissions(id, model_profile_id, permission_type, granted,
                   scope_json, valid_until, created_at, updated_at)
                   VALUES ($1,$2,$3,$4,$5::jsonb,$6,NOW(),NOW())
                   ON CONFLICT(model_profile_id, permission_type) DO UPDATE SET
                     granted=EXCLUDED.granted, scope_json=EXCLUDED.scope_json,
                     valid_until=EXCLUDED.valid_until, updated_at=NOW()""",
                permission_id, model_profile_id, permission_type, granted, scope_json, valid_until,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO model_permissions(id, model_profile_id, permission_type, granted,
               scope_json, valid_until, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(model_profile_id, permission_type) DO UPDATE SET
                 granted=excluded.granted, scope_json=excluded.scope_json,
                 valid_until=excluded.valid_until, updated_at=excluded.updated_at""",
            (permission_id, model_profile_id, permission_type, int(granted), scope_json,
             valid_until, utc_now(), utc_now()),
        )
        await db.commit()


async def model_permission_list(model_profile_id: str) -> list[dict[str, Any]]:
    """Return permissions for one model profile."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM model_permissions WHERE model_profile_id = $1 ORDER BY permission_type", model_profile_id)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM model_permissions WHERE model_profile_id = ? ORDER BY permission_type", (model_profile_id,))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]
