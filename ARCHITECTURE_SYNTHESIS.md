# OMNI-CORE NEXUS – Architektur-Synthese (13 Repos)

Audit-Basis: `/tmp/nexus_audit/*` (depth=1). `claw-code-omniroute` war nicht klonbar (403/disabled) – Auth/Proxy-Logik wurde aus `opencode-omniroute-auth`, `CLIProxyAPI`, `app/services/omniroute_management.py` und `app/services/ai_orchestrator.py` synthetisiert.

## Übernommene Module & Vereinheitlichung

| Subsystem | Quelle(n) | Übernommen / adaptiert | Vereinheitlichung / Duplikat-Elimination |
|---|---|---|---|
| Event-Sourcing Multi-Agent-Bus | ESAA, aiox-core, Observer, `services/event_bus.py` | ESAA: append-only Event-Log, deterministische Projektion/Traceability, fail-closed Contracts/Policies, Replay/Hash-Ideen. aiox-core: Agent/Squad/Workflow-Orchestrierung (Plan→Tasks→Artefakte). Observer: Supervisor-Metriken (Latenz, Fehler, Token/Health), Self-Heal-Restart-Logik. Bestehend: publish/subscribe, DLQ, correlation/trace IDs. | **Eine** Event-Spine statt konkurrierender Loops: `app/nexus/event_bus/store.py` (immutable `nexus_events` + Projektion) + `app/nexus/observers/supervisor.py`. Bestehender `EventBus` bleibt Transport; Nexus persistiert fachliche Domain-Events idempotent (kein zweites paralleles Bus-System). |
| OmniRoute Native Auth/Proxy & Skills | opencode-omniroute-auth, CLIProxyAPI, skills, knowledge-catalog, `omniroute_client/management/orchestrator` | opencode-auth: `/v1` Bearer, `/v1/models`, Combo-/Model-Refresh, BaseURL-Alignment. CLIProxyAPI: Konzept OpenAI-kompatibler Upstream-Bridge/Fallback (als optionaler Provider hinter OmniRoute). skills: SKILL.md-Workflows/Guardrails. knowledge-catalog: RAG/Katalog-Konzept für Marken/Material/Kategorie-Regeln. | **Kein eigener Proxy-Stack in Middleware** (eliminiert Duplikat zu OmniRoute/CLIProxyAPI): Middleware nutzt zentral `AIOrchestrator`/OmniRoute-Clients. `app/nexus/skills/registry.py` kapselt modulare Skills; Knowledge als versionierte Regeln/Docs im Store (RAG-light, erweiterbar). |
| Omnichannel WhatsApp/Chat/Voice CRM | wacli, WaCalls, chat-js, `services/messaging_bridge.py`, `crm_service` | wacli: CLI/JSON für WA sync/send/messages (Bridge). WaCalls: Call-Event/Session/VoIP-Domänenmodell (Events/Metadaten, keine Media-Reimpl.). chat-js: Admin/Storefront Chat-UI-Muster (Next.js/AI-SDK) → hier als integrierte FastAPI-Endpoints + Nexus-UI-Panel. | **Ein** Adapter `app/nexus/omnichannel/adapter.py` normalisiert Kanäle (whatsapp/chat/call/shopware) ins einheitliche Schema + Event-Log. UI nicht doppelt gebaut; API-first, einbettbar in WebGUI/Shopware. |
| Social-Commerce Pipeline | social-auto-upload, gstack, `services/social_auto_upload.py` | social-auto-upload: `sau` CLI, Plattform-Capabilities, Account/Headless-Flow, plan/execute. gstack: Sprint-/Review-/Release-Disziplin für Freigabe/Qualitäts-Gates. | **Eine** Pipeline `app/nexus/skills/social_publish.py` + Queue-State in `nexus_events`/Jobs; Copy via `omni-content-seo`, Media via `omni-vision-photo`/`omni-video-gen`, Publish nur explizit/gated. Kein zweiter Uploader-Codepfad. |
| Control Dashboard & Live-Observer UI | gstack, Observer, bestehende Templates/UI | Observer: Live-Status/Health. gstack: übersichtliche Boards/Checklisten. | **Ein** Menü „NEXUS Multi-Agent Control“ via API + `app/nexus/ui/views.py` (HTML-Panel) in bestehendes WebGUI eingehängt. |

## Sicherheits- & Routing-Invarianten
- **DSGVO/PII-Firewall:** Eingänge mit PII (WA-Nummer, Anruf-Metadaten, Bestellnummer, E-Mail/Kunde) → `omni-local-gdpr` ausschließlich (kein Cloud-Fallback), erzwungen durch `AIOrchestrator.detect_pii/route`.
- **Zahlungs-/Fulfillment-Guard:** `skill_erp_guard`/CRM-Prüfung vor Kundenantworten; keine Freigabe bei unklarem/fehlendem `payment_status`.
- **Append-only Events:** `nexus_events` immutable; Korrekturen via Folge-Events (ESAA-Prinzip).
- **Secrets:** keine Credentials in Repo/Logs; OmniRoute intern (`192.168.176.22:20128`), mgmt/v1 Tokens getrennt.

## Ziel-Module (app/nexus)
- `event_bus/` store, models, projection
- `skills/` registry + matterhorn_enrich, media_studio, shopware_sync, erp_guard, social_publish
- `omnichannel/` adapter, whatsapp(wacli), voice(call events), chat, shopware hooks
- `observers/` supervisor, metrics, health
- `ui/` routes/views for „NEXUS Multi-Agent Control“
