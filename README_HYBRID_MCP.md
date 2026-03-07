# Hybrid MCP Server — OpenManus + Manus API

GARZA OS integration that combines fast local agents with Manus AI's autonomous cloud execution.

## Architecture

```
Claude (GARZA OS)
    │
    └── Hybrid MCP Server (Railway)
            │
            ├── Sync Layer — OpenManus local agents
            │     bash, editor, terminate
            │     ↳ Direct execution, results inline
            │
            └── Async Layer — Manus SaaS API
                  manus_create_task   → fire-and-forget
                  manus_get_task      → poll for results
                  manus_list_tasks    → browsing history
                  manus_upload_file   → attach context
                  manus_list_files    → file management
                  manus_create_webhook → push notifications via n8n
```

## When to Use Each Layer

| Use case | Tool |
|----------|------|
| Run shell commands | `bash` |
| Read / write files | `editor` |
| Signal task done | `terminate` |
| Deep research, multi-step web tasks | `manus_create_task` (agent, quality) |
| Quick research or data lookup | `manus_create_task` (agent, speed) |
| Check if a Manus task finished | `manus_get_task` |
| List past Manus tasks | `manus_list_tasks` |
| Give Manus a document to work with | `manus_upload_file` → `manus_create_task` |
| Auto-notify n8n on completion | `manus_create_webhook` |

## Railway Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LLM_MODEL` | ✅ | e.g. `gpt-4o` or `claude-sonnet-4-5` |
| `LLM_BASE_URL` | ✅ | e.g. `https://api.openai.com/v1` |
| `LLM_API_KEY` | ✅ | Your LLM provider API key |
| `MANUS_API_KEY` | ✅ | From manus.im → Settings → API |
| `MANUS_GMAIL_CONNECTOR_ID` | Optional | Manus connector ID for Gmail access |
| `MANUS_NOTION_CONNECTOR_ID` | Optional | Manus connector ID for Notion access |
| `MANUS_GCAL_CONNECTOR_ID` | Optional | Manus connector ID for Google Calendar access |

## Deployment

### 1. Fork & connect to Railway

Fork `FoundationAgents/OpenManus` (already done if you're reading this).
In Railway → New Project → Deploy from GitHub → select `itsablabla/OpenManus`.

### 2. Set environment variables

Add all required variables from the table above in Railway → Variables.

### 3. Deploy

Railway auto-deploys on push. `entrypoint.sh` runs at startup, writes `config/config.toml`
from env vars, then starts the MCP server.

### 4. Verify startup logs

```
[entrypoint] Writing config/config.toml from environment variables...
[entrypoint] config.toml written. Starting MCP server...
[CONFIG] LLM[default] model='gpt-4o'  base_url='https://api.openai.com/v1'  api_type='openai'
```

## Add to Claude (claude_desktop_config.json)

```json
{
  "mcpServers": {
    "openmanus-hybrid": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-sse", "https://your-railway-url.railway.app/sse"]
    }
  }
}
```

## Diagnostic Test

```bash
MANUS_API_KEY=your_key python tests/test_manus_api_tools.py
```

Runs create → poll → list → webhook test directly against Manus API.

## Cost Reference

| Profile | Credits | Best for |
|---------|---------|----------|
| `speed` | ~50-100 credits | Quick research, summaries |
| `quality` | ~150-300 credits | Deep research, complex multi-step tasks |

Credits are consumed per task. See manus.im/pricing for current rates.

## File Map

```
entrypoint.sh              ← writes config.toml at container startup
railway.json               ← Railway deployment config
Dockerfile                 ← CMD fixed to ./entrypoint.sh
app/
  config.py                ← env var substitution + debug logging
  mcp/
    server.py              ← hybrid server (sync + 6 async tools)
    manus_client.py        ← Manus API client
    manus_models.py        ← Pydantic input models
tests/
  test_manus_api_tools.py  ← diagnostic script
```
