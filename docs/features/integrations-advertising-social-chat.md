# Integrationen: Werbung, Social Auto Upload, WhatsApp, Chat UI, OmniRoute/CLIProxy

## Überblick
Diese Middleware-Erweiterung kombiniert mehrere Upstream-Ideen zu einem e-commerce-tauglichen Stack (auto/smart-first über OmniRoute):

- **Werbung & Social Publishing** (`services/social_auto_upload.py`, `config/integrations/social_auto_upload.json`)
  - Adaptiert `dreammis/social-auto-upload` (CLI `sau`, Plattform-Uploader, Account-Cookies, headless): Planung/Generierung von Kampagnen-Copies & Publish-Jobs je Plattform (Douyin, Bilibili, Xiaohongshu, Kuaishou, …).
  - Copy/Hashtags/Skripte werden über `AIOrchestrator` + OmniRoute-Combos (z. B. `omni-ecom-marketing-smart`, `auto/smart`) erzeugt; Ausführung erfolgt explizit über `sau`-CLI-Bridge (kein implizites Posten, keine Credentials in DB).

- **WhatsApp/CRM-Messaging** (`services/messaging_bridge.py`, `WacliBridge`)
  - Adaptiert `steipete/wacli` (CLI, JSON, lokale Store/Session) als Kanal-Bridge für CRM-Ingest/Outbound. `enabled=false` bis Binary+Session bereit.

- **Admin-Chatfenster & Shopware-Kundenchat** (`services/messaging_bridge.py` → `admin_chat_reply`, `storefront_chat_reply`)
  - Server-seitige Chat-Endpunkte für (a) Admin-Copilot (Katalog/Marketing/CRM/Creative/OmniRoute-Ops, schreibende Aktionen nur als Plan/Freigabe) und (b) Storefront-Assistent (guarded, kein interner Datenabfluss).
  - UI-Integration: `chat-js` (Next.js/AI-SDK) kann als Frontend/Widget angebunden werden; Backend zeigt auf Omni-CORE-Endpoints, diese routen über OmniRoute. So entsteht Adminpanel-Chat und Shopware-Kontakt-Widget mit denselben AI-Funktionen/Policies.

- **OmniRoute-Auth & Modellzugriff** (wichtig: `opencode-omniroute-auth`)
  - Plugin verbindet OpenCode via `/connect` gegen OmniRoute (`/v1`, `/v1/models`, Combos). Für unseren Stack: interner LAN-BaseURL, API-Key via `/connect`/auth store, `apiMode=chat` (Default), Combos/auto-Modelle (`auto/smart`, `omni-ecom-*`) in OmniRoute bereitstellen; Middleware/Agents nutzen dieselben Combo-Namen.

- **CLIProxyAPI** (`router-for-me/CLIProxyAPI`)
  - Optionaler Upstream: stellt CLI-basierte Provider als OpenAI/Gemini/Claude-kompatible API bereit. Kann als Provider hinter OmniRoute oder als separater Endpoint für spezifische Modelle betrieben werden (Routing/Failover über OmniRoute bevorzugt).

- **ESAA (Event-Sourcing-Agent-Architecture)**
  - Muster für deterministische, auditierbare Agent-Workflows (append-only Events, Projektionen, fail-closed Policies). Passt zu `services/event_bus.py` + `jobs`/`system_logs`: Publish-/Kampagnen-/Agent-Aktionen als Events protokollieren, Replay/Tracing über `correlation_id`/`trace_id`.

- **AIOX-Core**
  - Agent/Squad/Workflow-Orchestrierungsideen (Plan → spezialisierte Agents/Tasks → Artefakte). Übertragbar auf Marketing-/Content-/Ops-Squads, die unsere Combos und Tools nutzen.

## Geplante API-Erweiterungen (nächster Schritt in `main.py`/DB)
- `GET /api/social/platforms`, `POST /api/social/ad-copy`, `POST /api/social/publish/plan`, `POST /api/social/publish/execute` (RBAC `ai.generate`/`exports.execute` + Audit)
- `POST /api/chat/admin`, `POST /api/chat/public` (rate-limited, guarded)
- `GET /api/messaging/whatsapp/status`, `POST /api/messaging/whatsapp/send`
- Persistenz: `marketing_campaigns`, `social_publish_jobs`, `chat_sessions/messages` (append-only) – kompatibel zu bestehendem Schema-Migrationsstil.

## Sicherheit & Betrieb
- PII -> Orchestrator erzwingt lokalen/privacy-Pfad; keine Secrets in Repo/Logs.
- Social/WhatsApp: Credentials/Cookies nur in geschützten Host-Pfaden (`/var/lib/omnicore/...`, chmod 600, non-root), Publish nur mit expliziter Bestätigung.
- OmniRoute nur intern binden (LAN/localhost), nicht `0.0.0.0` öffentlich; API-Key/Token trennen (v1 vs mgmt).
- `wacli`/`sau` benötigen installierte Browser/Deps; auf Alpine LXC gezielt vorbereiten (Playwright/Patchright deps, Chromium/Headless).
