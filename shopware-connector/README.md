# Omni-Core Shopware Connector

Vorbereitete, eigenständige Integrationsstruktur für die Anbindung von Shopware 6 an die Omni-Core-Middleware.

## Grundsätze

- **Middleware ist führendes System** für Produktdaten, KI-Anreicherung, Preislogik und Multi-Channel-Vertrieb.
- **Nur offizielle Shopware-APIs** (Admin API/Store API) für externe Schreibzugriffe.
- **Keine direkten Schreibzugriffe** auf Shopware-Datenbanktabellen.
- Dedizierter technischer Benutzer, Least-Privilege, getrennt vom Shop-Administrator.

## Module

- `authentication/` – OAuth2 Client Credentials, Token-Refresh, Secret-Handling
- `products/`, `variants/`, `categories/` – Produkt-, Varianten-, Kategoriesync
- `media/` – Medienimport/-zuordnung
- `inventory/`, `pricing/` – Bestands- und Preisupdates
- `orders/`, `customers/` – Order-Import, Kundensync
- `webhooks/` – Webhook-Registrierung/Verarbeitung
- `health/` – Verbindungs- und API-Health
- `mapping/` – Feld-/Kanal-Mapping (maschinenlesbar)
- `validation/` – Payload-Validierung vor API-Calls
- `retry/` – Retry/Backoff/Idempotenz
- `logging/` – Strukturierte Logs ohne Secrets

## Konfiguration

`config/connector.json` wird durch `configure-middleware.sh` erzeugt (maschinenlesbar, versionierbar).
Secrets liegen separat unter dem konfigurierten `security.secrets_dir` (nicht in Git).
