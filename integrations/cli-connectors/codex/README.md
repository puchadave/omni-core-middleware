# Codex CLI + OmniRoute

Use OmniRoute’s OpenAI-compatible endpoint as the base URL for OpenAI-style clients.

```bash
source config.example.env  # or export OPENAI_BASE_URL / OPENAI_API_KEY
```

- Base: `http://192.168.176.22:20128/v1`
- Models: `auto/coding`, `auto/smart`, `omni-ecom-coding-smart`
- For NEXUS/Proxmox ops, pair with `../scripts/nexus-cli.sh` (set `OMNICORE_*`).
