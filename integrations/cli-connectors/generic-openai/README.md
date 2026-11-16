# Generic OpenAI-Compatible Clients -> OmniRoute

Any CLI/SDK supporting OpenAI-compatible chat completions can use OmniRoute:

```bash
export OPENAI_BASE_URL=${OMNIROUTE_BASE_URL:-http://192.168.176.22:20128/v1}
export OPENAI_API_KEY=${OMNIROUTE_API_KEY}
```

## Smoke tests
```bash
# List models/combos
curl -sS "$OPENAI_BASE_URL/models" -H "Authorization: Bearer $OPENAI_API_KEY" | jq .

# Chat completion
curl -sS "$OPENAI_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto/smart","messages":[{"role":"user","content":"Say OK"}]}' | jq -r '.choices[0].message.content'
```

## NEXUS/Proxmox automation (middleware API)
```bash
export OMNICORE_BASE_URL=http://<middleware-ip>:8002
export OMNICORE_API_KEY=<INTERNAL_API_KEY>
../scripts/nexus-cli.sh nexus-status
../scripts/nexus-cli.sh pve-plan "OmniRoute LXC 2c/2GB alpine amd64"
../scripts/nexus-cli.sh orchestrate code "Add tests for firewall planner"
```

## Model/Combo hints
- General/coding: `auto/smart`, `auto/coding`, `omni-ecom-coding-smart`
- Fast/cheap: `auto/fast`, `auto/cheap`
- Ops/planning: `omni-admin-agent`
- Privacy-sensitive: `omni-local-gdpr`
