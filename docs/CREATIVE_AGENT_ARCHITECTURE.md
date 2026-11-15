# Creative Agent Architecture – Omni-Core

## Purpose

Defines the architecture for the AI Creative Studio and the operational agent layer that will run inside the Omni-Core admin panel.

## Agent Operating Model

- The creative agent is an operational component of the admin panel, not a standalone chatbot.
- All model access must pass through the central OmniRoute abstraction.
- The agent must decompose a natural-language request into registered tool calls.
- Tool calls require JSON schemas, validation, permissions, error handling, and audit logging.
- No arbitrary database queries or shell commands may be issued by the agent.

## Existing Integration Points

- `/api/photoshoot/start`, `/api/photoshoot/status/{shootId}`, `/api/photoshoot/results/{shootId}`.
- `/api/models` and `/api/marketing/products/{sku}/generate-content`.
- OmniRoute provider, model, routing, policy, cost, budget, health, request, error, and audit endpoints.
- Product, bundle, settings, log, and import APIs.
- `services/ai_photoshooting.py` provider abstraction.
- `services/model_reference.py` static model manifest and reference paths.

## Required Agent Components

- Creative Agent UI with chat, upload, product/bundle/model/scene selection, job status, preview, variant comparison, cost display, approval, and regeneration.
- Agent Manager with agent profiles, allowed tools, model configuration, system prompts, permissions, budgets, time limits, status, and run history.
- Agent Tool Registry with registered tools only.
- Persistent conversation and task context.
- Creative project, job, output, and approval state.
- Async generation jobs with IDs, progress, provider status, retry, timeout, cost, errors, result files, and previews.
- Human-in-the-loop approval before publishing or other sensitive actions.

## Required Tool Set

- `search_products`
- `get_product_details`
- `get_product_images`
- `search_models`
- `get_model_reference`
- `create_bundle`
- `analyze_product`
- `create_image_prompt`
- `generate_image`
- `edit_image`
- `generate_video`
- `create_storyboard`
- `compare_generated_assets`
- `save_asset`
- `create_campaign`
- `calculate_campaign_cost`
- `request_approval`
- `publish_approved_asset`

## Product Fidelity Requirements

The agent must retrieve original product images and attributes, include product details in prompts, select compatible model references, generate candidates, compare them against originals, identify critical deviations, and block automatic publishing when critical deviations exist.

Checked attributes include color, cut, pattern, fabric structure, sleeves, collar, neckline, buttons, closures, logos, prints, seams, and proportions.

Quality must be separated into visual quality, product fidelity, model consistency, scene quality, and technical image quality.

## Model Identity Requirements

Model profiles must support own identities, fictional brand models, and real persons with documented consent. Profiles require reference images, versions, visual traits, pose preferences, permitted use, consent status, validity period, rights holder, and approval documents.

Generation must be blocked when rights are missing or expired. Real-person deepfake use requires documented consent and visible disclosure where required.

## Transparency and Provenance

Generated content must store generation timestamp, provider, model, prompt version, references, edit history, publication status, and machine-readable provenance metadata where supported. Visible disclosure must be used when required. C2PA/Content Credentials should be supported when infrastructure permits.

## Testing

Use mock providers only. No paid generation may occur during automated tests. Tests must cover tool validation, permissions, provider failures, job retry, approval gating, and audit logging.
