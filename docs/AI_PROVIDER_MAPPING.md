# AI Provider Mapping – Omni-Core

## Purpose

Maps existing OmniRoute and AI provider capabilities to the planned agent, creative, intelligence, and social-commerce modules.

## Existing Provider Layer

- `ai_providers` stores provider type, endpoint, encrypted API key, auth type, active state, priority, rate limits, timeout, retry, circuit breaker, capabilities, health status, and timestamps.
- `ai_provider_credentials` stores additional encrypted credentials.
- `ai_models` stores provider, model ID/name/version, category, capabilities, context window, input/output/image pricing, rate limit, active/discoverable state.
- `ai_model_capabilities` stores capability name/value, confidence, and source (`discovered` or `manual`).
- `ai_routing_rules` supports lowest cost, highest quality, lowest latency, provider priority, capability matching, and weighted distribution.
- `ai_routing_policies` binds a workflow name to a rule and optional fallback.
- `ai_workflows` and `ai_workflow_configs` define workflow execution and step configuration.
- `ai_usage_records`, `ai_cost_records`, `ai_budgets`, `ai_provider_health`, `ai_request_logs`, `ai_error_logs`, and `ai_audit_logs` provide observability and cost control.

## Existing OmniRoute APIs

- Provider management: create, list, test, disable/enable, update, health check all.
- Model management: create and list, with provider/category filters.
- Routing: create/list rules and policies, select provider.
- Cost: calculate text/image/video cost.
- Statistics: OmniRoute stats.
- Client: health check, trend analysis, SEO optimization.

## Capability Categories

Provider type and model category currently support:

- `text`
- `image`
- `video`
- `embedding`
- `multimodal`

The agent must distinguish text, vision, image generation, image editing, video generation, and audio/voice models. These distinctions must be represented through actual provider/model metadata or administratively confirmed capability records, not inferred from model names.

## Mapping to Agent Tasks

- Text and vision models: prompt generation, analysis, classification, SEO, campaign copy, quality checks.
- Image generation models: photo studio output.
- Image editing models: controlled edits and corrections.
- Video generation models: video studio output.
- Embedding models: similarity, deduplication, matching, trend correlation.
- Multimodal models: combined text/image/video analysis where metadata confirms support.

## Routing and Cost Controls

- Routing policy selection by workflow.
- Fallback rules.
- Circuit breaker success/failure tracking.
- Cost calculation for text, image, and video.
- Usage and cost records.
- Budgets by daily, monthly, provider, or workflow with warning thresholds and exceed actions.
- Provider health and request/error logs.

## Gaps

- No image/video generation endpoint is exposed through the central OmniRoute client; photoshooting uses its own provider implementation.
- `OmniRouteClient` has no generic chat/completion, vision analysis, image edit, video, embedding, or audio method.
- No central capability discovery or validation beyond manual provider/model records.
- No agent-specific tool registry, execution state, or cost preflight.
- Social platform adapters are not implemented.
- Provider/model capabilities are not yet mapped to concrete creative studio workflows.

## Requirements for Expansion

- Add model capabilities only from provider metadata, endpoint responses, or explicit admin confirmation.
- Record unknown costs as unknown instead of estimating them.
- Keep model selection provider-agnostic through OmniRoute.
- Add cost preflight before expensive generation when provider pricing is available.
- Expose provider health, cost, budget, and capability data to the agent manager.
