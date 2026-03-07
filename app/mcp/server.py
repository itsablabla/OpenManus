"""
Hybrid MCP Server for GARZA OS — OpenManus + Manus API.

Synchronous layer  : bash, browser, editor, terminate (OpenManus local agents)
Asynchronous layer : manus_create_task, manus_get_task, manus_list_tasks,
                     manus_upload_file, manus_list_files, manus_create_webhook,
                     garza_status
                     (Manus SaaS API — fire-and-forget, poll for results)

IMPROVEMENTS (v2):
  - Auth enforcement: Bearer token validated on every MCP request
  - manus_get_task: clean human-readable output, no raw JSON arrays
  - garza_status: NL query → human digest (the "what ran today?" tool)
  - manus_list_tasks: better timestamp + title formatting
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from app.mcp.manus_client import handle_api_error, manus_request
from app.mcp.manus_models import (
    CreateTaskInput,
    CreateWebhookInput,
    GetTaskInput,
    ListTasksInput,
)

logger = logging.getLogger(__name__)
mcp = FastMCP("OpenManus Hybrid MCP Server")

# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------

_AUTH_TOKEN = os.environ.get("MCP_SERVER_AUTH_TOKEN", "")


def _check_auth(request: Request) -> bool:
    """Return True if request carries a valid Bearer token (or auth is disabled)."""
    if not _AUTH_TOKEN:
        return True  # Auth disabled — no token configured
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == _AUTH_TOKEN:
        return True
    # Also allow token as query param for SSE clients that can't set headers
    if request.query_params.get("token") == _AUTH_TOKEN:
        return True
    return False


# ---------------------------------------------------------------------------
# OAuth2 well-known endpoints (required for Nango mcp-generic)
# ---------------------------------------------------------------------------

_BASE_URL = "https://" + os.environ.get(
    "RAILWAY_PUBLIC_DOMAIN", "openmanus-mcp-production.up.railway.app"
)


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def oauth_authorization_server(request: Request) -> JSONResponse:
    """RFC 8414 — OAuth 2.0 Authorization Server Metadata."""
    return JSONResponse({
        "issuer": _BASE_URL,
        "authorization_endpoint": f"{_BASE_URL}/oauth/authorize",
        "token_endpoint": f"{_BASE_URL}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["mcp"],
    })


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def oauth_protected_resource(request: Request) -> JSONResponse:
    """RFC 9728 — OAuth 2.0 Protected Resource Metadata."""
    return JSONResponse({
        "resource": _BASE_URL,
        "authorization_servers": [_BASE_URL],
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://github.com/itsablabla/OpenManus",
    })


@mcp.custom_route("/oauth/authorize", methods=["GET"])
async def oauth_authorize(request: Request) -> RedirectResponse:
    """OAuth2 authorization endpoint."""
    redirect_uri = request.query_params.get("redirect_uri", "")
    state = request.query_params.get("state", "")
    code = os.environ.get("MCP_SERVER_AUTH_TOKEN", "")
    return RedirectResponse(url=f"{redirect_uri}?code={code}&state={state}")


@mcp.custom_route("/oauth/token", methods=["POST"])
async def oauth_token(request: Request) -> JSONResponse:
    """OAuth2 token endpoint — exchanges auth code for access token."""
    form = await request.form()
    code = str(form.get("code", ""))
    token = code or os.environ.get("MCP_SERVER_AUTH_TOKEN", "")
    return JSONResponse({
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": 31536000,
        "scope": "mcp",
    })


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Health check endpoint."""
    return JSONResponse({"status": "ok", "service": "openmanus-mcp", "ready": True})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_timestamp(ts) -> str:
    """Format a unix timestamp or ISO string to human-readable."""
    if not ts:
        return ""
    try:
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            s = str(ts)[:19].replace("T", " ")
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ts)[:19]


def _extract_task_output(output) -> str:
    """Convert Manus output to clean readable text regardless of format."""
    if not output:
        return ""
    # If it's a list of message dicts (conversation array format)
    if isinstance(output, list):
        parts = []
        for msg in output:
            role = msg.get("role", "")
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "output_text":
                        text = block.get("text", "").strip()
                        if text and role == "assistant":
                            parts.append(text)
            elif isinstance(content, str) and role == "assistant":
                parts.append(content.strip())
        return "\n\n".join(parts) if parts else str(output)[:500]
    # If it's already a string
    if isinstance(output, str):
        return output.strip()
    return str(output)[:500]


# ---------------------------------------------------------------------------
# Synchronous tools (bash, editor, terminate)
# ---------------------------------------------------------------------------

@mcp.tool()
async def bash(command: str) -> str:
    """Execute a bash command and return its output."""
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    result = stdout.decode() + stderr.decode()
    return result.strip()


@mcp.tool()
async def editor(command: str, path: str, content: str = "") -> str:
    """Read or write a file. command: \'view\' | \'create\' | \'str_replace\'."""
    if command == "view":
        try:
            with open(path, "r") as f:
                return f.read()
        except FileNotFoundError:
            return f"File not found: {path}"
    elif command == "create":
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
        with open(path, "w") as f:
            f.write(content)
        return f"Created: {path}"
    elif command == "str_replace":
        with open(path, "r") as f:
            text = f.read()
        old, new = content.split("\n---REPLACE---\n", 1) if "\n---REPLACE---\n" in content else (content, "")
        text = text.replace(old, new, 1)
        with open(path, "w") as f:
            f.write(text)
        return f"Replaced in {path}"
    return f"Unknown command: {command}"


@mcp.tool()
async def terminate(message: str = "") -> str:
    """Signal task completion to the orchestrator."""
    return f"Task complete. {message}".strip()


# ---------------------------------------------------------------------------
# Manus API tools (async, fire-and-forget / poll)
# ---------------------------------------------------------------------------

@mcp.tool()
async def manus_create_task(
    prompt: str,
    task_mode: str = "agent",
    agent_profile: str = "speed",
    file_ids: str = "",
    use_gmail_connector: bool = False,
    use_notion_connector: bool = False,
    use_gcal_connector: bool = False,
) -> str:
    """
    Submit a long-running task to Manus AI for autonomous execution.

    Returns a task_id immediately. Use manus_get_task to poll for results.

    Args:
        prompt: The task description — be specific and detailed
        task_mode: "agent" (default, uses tools) or "chat" (pure LLM)
        agent_profile: "speed" (fast, cheaper) or "balanced" (higher quality)
        file_ids: Comma-separated file IDs from manus_upload_file (optional)
        use_gmail_connector: Give Manus read/send access to Gmail
        use_notion_connector: Give Manus access to Notion
        use_gcal_connector: Give Manus access to Google Calendar
    """
    try:
        connectors = []
        if use_gmail_connector:
            connectors.append("gmail")
        if use_notion_connector:
            connectors.append("notion")
        if use_gcal_connector:
            connectors.append("gcal")

        fids = [f.strip() for f in file_ids.split(",") if f.strip()] if file_ids else []

        payload: dict = {
            "task": {
                "prompt": prompt,
                "mode": task_mode,
                "agent_profile": agent_profile,
            }
        }
        if fids:
            payload["task"]["file_ids"] = fids
        if connectors:
            payload["task"]["connectors"] = connectors

        result = await manus_request("POST", "/tasks", json=payload)
        task_id = result.get("id") or result.get("task_id", "unknown")
        status = result.get("status", "pending")

        return (
            f"Task created successfully.\n"
            f"task_id : {task_id}\n"
            f"status  : {status}\n"
            f"Tip     : Call manus_get_task(task_id=\'{task_id}\') in 2-3 minutes to check progress."
        )
    except Exception as e:
        return handle_api_error(e)


@mcp.tool()
async def manus_get_task(task_id: str) -> str:
    """
    Poll the status and output of a Manus task.

    Returns clean, human-readable output — not raw JSON.

    Args:
        task_id: The task ID returned by manus_create_task
    """
    try:
        result = await manus_request("GET", f"/tasks/{task_id}")
        status = result.get("status", "unknown")
        raw_output = result.get("output") or result.get("result") or result.get("messages") or ""
        artifacts = result.get("artifacts", [])
        credits_used = result.get("credits_used")
        error_msg = result.get("error_message") or result.get("error") or ""
        created_at = _fmt_timestamp(result.get("created_at", ""))
        completed_at = _fmt_timestamp(result.get("completed_at", ""))

        # Status icon
        icon = {"completed": "✅", "failed": "❌", "running": "⏳", "pending": "🕐"}.get(status, "❓")

        out = [
            f"{icon} Task: {task_id}",
            f"   Status    : {status}",
        ]
        if created_at:
            out.append(f"   Created   : {created_at}")
        if completed_at:
            out.append(f"   Completed : {completed_at}")
        if credits_used is not None:
            out.append(f"   Credits   : {credits_used}")

        # Clean output
        clean_output = _extract_task_output(raw_output)
        if clean_output:
            out.append(f"\n📄 Output:\n{clean_output}")

        if artifacts:
            out.append(f"\n📎 Artifacts ({len(artifacts)}):")
            for a in artifacts[:10]:
                out.append(f"   - {a.get('name', 'file')} → {a.get('url', '(no url)')}")

        if error_msg:
            out.append(f"\n⚠️  Error: {error_msg}")

        if status in ("pending", "running"):
            out.append("\n⏳ Still in progress — check again in 1-2 minutes.")

        return "\n".join(out)
    except Exception as e:
        return handle_api_error(e)


@mcp.tool()
async def manus_list_tasks(
    limit: int = 20,
    status_filter: str = "",
    offset: int = 0,
) -> str:
    """
    List recent Manus tasks.

    Args:
        limit: Number of tasks to return (1-100, default 20)
        status_filter: Optional filter — pending | running | completed | failed
        offset: Pagination offset (default 0)
    """
    try:
        params: dict = {"limit": limit, "offset": offset}
        if status_filter:
            params["status"] = status_filter

        result = await manus_request("GET", "/tasks", params=params)
        tasks = result.get("tasks", result.get("data", []))
        has_more = result.get("has_more", False)

        if not tasks:
            return "No tasks found."

        lines = [f"Tasks ({len(tasks)} returned):"]
        for t in tasks:
            tid = t.get("id", t.get("task_id", "?"))
            tstatus = t.get("status", "?")
            icon = {"completed": "✅", "failed": "❌", "running": "⏳", "pending": "🕐"}.get(tstatus, "❓")
            created = _fmt_timestamp(t.get("created_at", ""))
            meta = t.get("metadata") or {}
            title = meta.get("task_title") or (t.get("prompt", "") or "")[:60]
            url = meta.get("task_url", "")
            url_str = f"  {url}" if url else ""
            lines.append(f"  {icon} {tid}  [{tstatus}]  {created}  {title!r}{url_str}")

        if has_more:
            lines.append(f"\\nMore tasks available — use offset={offset+limit} for next page.")

        return "\\n".join(lines)
    except Exception as e:
        return handle_api_error(e)


@mcp.tool()
async def garza_status(query: str = "what ran today") -> str:
    """
    Natural language Manus status digest for GARZA OS.

    Ask anything about recent Manus activity in plain English:
      - "what ran today"
      - "any failed tasks?"
      - "what did Manus do this week"
      - "show me running tasks"
      - "how many credits did I use today"
      - "summarize the last 5 completed tasks"

    Returns a conversational paragraph — no raw data.

    Args:
        query: Natural language question about Manus tasks (default: "what ran today")
    """
    import httpx as _httpx

    try:
        # Fetch recent tasks (all statuses, last 50)
        result = await manus_request("GET", "/tasks", params={"limit": 50})
        tasks = result.get("tasks", result.get("data", []))

        if not tasks:
            return "No Manus tasks found. The queue is empty."

        # Build a compact summary for the LLM
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        task_lines = []
        for t in tasks:
            tid = t.get("id", "?")
            status = t.get("status", "?")
            created = _fmt_timestamp(t.get("created_at", ""))
            meta = t.get("metadata") or {}
            title = meta.get("task_title") or (t.get("prompt", "") or "")[:80]
            credits = t.get("credits_used", "")
            credits_str = f" [{credits} credits]" if credits else ""
            task_lines.append(f"- {tid} [{status}] {created} {title!r}{credits_str}")

        task_summary = "\\n".join(task_lines)

        # Use the LLM proxy (same one the OpenManus agents use)
        llm_base = os.environ.get("LLM_BASE_URL", "")
        llm_key = os.environ.get("LLM_API_KEY", "")
        llm_model = os.environ.get("LLM_MODEL", "gpt-4.1-mini")

        if not llm_base or not llm_key:
            # Fallback: return structured text if no LLM available
            statuses = {}
            for t in tasks:
                s = t.get("status", "unknown")
                statuses[s] = statuses.get(s, 0) + 1
            counts = ", ".join(f"{v} {k}" for k, v in sorted(statuses.items()))
            return f"Manus has {len(tasks)} recent tasks: {counts}. Ask me to filter by status or show details on a specific task."

        system_prompt = (
            f"You are GARZA OS, a personal AI operating system assistant. "
            f"Today is {today} UTC. "
            f"Answer the user\'s question about their Manus AI task history in 2-4 sentences of natural, conversational prose. "
            f"Be specific about what tasks did. Don\'t use bullet points. "
            f"If credits are available, mention the total. "
            f"If there are failures, call them out clearly."
        )

        user_prompt = (
            f"User question: {query}\\n\\n"
            f"Recent Manus task data (last {len(tasks)} tasks):\\n{task_summary}"
        )

        async with _httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"https://{llm_base.lstrip('https://').rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {llm_key}", "Content-Type": "application/json"},
                json={
                    "model": llm_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_tokens": 300,
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            answer = data["choices"][0]["message"]["content"].strip()

        return answer

    except Exception as e:
        return handle_api_error(e)


@mcp.tool()
async def manus_upload_file(filename: str, content_base64: str, content_type: str = "text/plain") -> str:
    """
    Upload a file to Manus and get a file_id for use in manus_create_task.

    Args:
        filename: Name of the file (e.g., \'report.pdf\')
        content_base64: Base64-encoded file content
        content_type: MIME type (default text/plain)
    """
    try:
        import base64
        import httpx

        # Step 1: Get presigned upload URL
        presign = await manus_request("POST", "/files", json={
            "filename": filename,
            "content_type": content_type,
        })
        upload_url = presign.get("upload_url", "")
        file_id = presign.get("id") or presign.get("file_id", "unknown")

        if upload_url:
            file_bytes = base64.b64decode(content_base64)
            async with httpx.AsyncClient() as client:
                await client.put(
                    upload_url,
                    content=file_bytes,
                    headers={"Content-Type": content_type},
                    timeout=30,
                )

        return (
            f"File uploaded.\\n"
            f"file_id      : {file_id}\\n"
            f"filename     : {filename}\\n"
            f"content_type : {content_type}\\n"
            f"Tip          : Pass file_ids=\'{file_id}\' to manus_create_task."
        )
    except Exception as e:
        return handle_api_error(e)


@mcp.tool()
async def manus_list_files(limit: int = 20, offset: int = 0) -> str:
    """
    List files uploaded to Manus.

    Args:
        limit: Number of files to return (default 20)
        offset: Pagination offset (default 0)
    """
    try:
        result = await manus_request("GET", "/files", params={"limit": limit, "offset": offset})
        files = result.get("files", result.get("data", []))

        if not files:
            return "No files found."

        lines = [f"Files ({len(files)}):"]
        for f in files:
            fid = f.get("id", f.get("file_id", "?"))
            fname = f.get("filename") or f.get("name", "unknown")
            size = f.get("size", "?")
            created = _fmt_timestamp(f.get("created_at", ""))
            lines.append(f"  {fid}  {fname}  {size} bytes  {created}")

        return "\\n".join(lines)
    except Exception as e:
        return handle_api_error(e)


@mcp.tool()
async def manus_create_webhook(
    url: str,
    events: str = "task.completed,task.failed",
) -> str:
    """
    Register a webhook to receive Manus task completion events.

    Use your n8n webhook trigger URL so GARZA OS is notified automatically
    when long-running tasks finish — no polling required.

    Args:
        url: Webhook endpoint URL (e.g., your n8n webhook trigger URL)
        events: Comma-separated events (default: task.completed,task.failed)
    """
    try:
        event_list = [e.strip() for e in events.split(",") if e.strip()]
        result = await manus_request("POST", "/webhooks", json={
            "webhook": {
                "url": url,
                "events": event_list,
            }
        })
        webhook_id = result.get("id") or result.get("webhook_id", "unknown")
        secret = result.get("secret", "(none)")

        return (
            f"Webhook registered.\\n"
            f"webhook_id : {webhook_id}\\n"
            f"url        : {url}\\n"
            f"events     : {event_list}\\n"
            f"secret     : {secret}\\n"
            f"Tip        : Store the secret in Railway env as MANUS_WEBHOOK_SECRET "
            f"and verify it in your n8n workflow."
        )
    except Exception as e:
        return handle_api_error(e)
