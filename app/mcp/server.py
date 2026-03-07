"""
Hybrid MCP Server for GARZA OS — OpenManus + Manus API.

Synchronous layer  : bash, browser, editor, terminate (OpenManus local agents)
Asynchronous layer : manus_create_task, manus_get_task, manus_list_tasks,
                     manus_upload_file, manus_list_files, manus_create_webhook,
                     garza_status (NL task digest with LLM summarization)
                     (Manus SaaS API — fire-and-forget, poll for results)

Sprint 1 fixes (2026-03-07):
  Fix 5 — Auth enforcement: Bearer token validated on every SSE/tool request
  Fix 1 — Enrich list output: credit_usage, created_at, updated_at surfaced
  Fix 4 — Prompt logging: task_id + prompt logged to /tmp/task_log.jsonl
  Fix 2 — Prompt + output in manus_get_task via /messages endpoint
  Fix 3 — garza_status NL tool: conversational task digest

Sprint 2 improvements (2026-03-07):
  Imp 1 — garza_status: LLM summarization pass → prose answer shaped to query
  Imp 2 — Human-readable timestamps across all task tools ("2h ago", "today at 11:43am")
  Imp 3 — Task duration calculation (updated_at − created_at → "completed in 23s")
  Imp 4 — Runaway task warning in garza_status (>30min running or >10 credits mid-run)
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
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
# Credit → USD conversion
# Manus pricing: $20/mo = 4,000 credits → $0.005 per credit (½ cent each)
# ---------------------------------------------------------------------------
_CREDITS_PER_DOLLAR = 200  # 4000 credits / $20

def _usd(credits: int) -> str:
    """Convert Manus credits to a human-readable USD string."""
    if not credits:
        return ""
    dollars = credits / _CREDITS_PER_DOLLAR
    if dollars < 0.01:
        return f"<$0.01"
    elif dollars < 1.0:
        return f"${dollars:.2f}"
    else:
        return f"${dollars:.2f}"

def _usd_label(credits: int) -> str:
    """Return a formatted cost label like '$0.43' or '$7.45'."""
    return _usd(credits) if credits else ""

# ---------------------------------------------------------------------------
# Fix 5 — Auth enforcement middleware
# ---------------------------------------------------------------------------

_PUBLIC_PATHS = {
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    "/oauth/authorize",
    "/oauth/token",
    "/",
}

_TASK_LOG_PATH = "/tmp/task_log.jsonl"


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests without a valid Bearer token (Fix 5)."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        expected_token = os.environ.get("MCP_SERVER_AUTH_TOKEN", "")
        if not expected_token:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        token = auth_header.removeprefix("Bearer ").strip()

        if token != expected_token:
            return Response(
                content=json.dumps({"error": "Unauthorized — invalid or missing Bearer token"}),
                status_code=401,
                media_type="application/json",
            )

        return await call_next(request)


@mcp.custom_route("/__auth_init__", methods=["GET"])
async def _auth_init_placeholder(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# OAuth2 well-known endpoints
# ---------------------------------------------------------------------------

_BASE_URL = "https://" + os.environ.get(
    "RAILWAY_PUBLIC_DOMAIN", "openmanus-mcp-production.up.railway.app"
)


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def oauth_authorization_server(request: Request) -> JSONResponse:
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
    return JSONResponse({
        "resource": _BASE_URL,
        "authorization_servers": [_BASE_URL],
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://github.com/itsablabla/OpenManus",
    })


@mcp.custom_route("/oauth/authorize", methods=["GET"])
async def oauth_authorize(request: Request) -> RedirectResponse:
    redirect_uri = request.query_params.get("redirect_uri", "")
    state = request.query_params.get("state", "")
    code = os.environ.get("MCP_SERVER_AUTH_TOKEN", "")
    return RedirectResponse(url=f"{redirect_uri}?code={code}&state={state}")


@mcp.custom_route("/oauth/token", methods=["POST"])
async def oauth_token(request: Request) -> JSONResponse:
    form = await request.form()
    code = str(form.get("code", ""))
    token = code or os.environ.get("MCP_SERVER_AUTH_TOKEN", "")
    return JSONResponse({
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": 31536000,
        "scope": "mcp",
    })


# ---------------------------------------------------------------------------
# Imp 2 — Human-readable timestamp helper
# ---------------------------------------------------------------------------

def _human_time(ts) -> str:
    """Convert Unix epoch or ISO string to human-readable relative time.
    
    Examples: '2h ago', 'today at 11:43am', 'yesterday at 3:15pm', 'Mar 5 at 9:00am'
    """
    if not ts:
        return ""
    try:
        if isinstance(ts, str):
            # Try parsing ISO format first
            ts_clean = ts.replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(ts_clean)
                epoch = dt.timestamp()
            except ValueError:
                epoch = float(ts)
        else:
            epoch = float(ts)
        
        now = time.time()
        diff = now - epoch
        
        if diff < 0:
            return "just now"
        elif diff < 60:
            return f"{int(diff)}s ago"
        elif diff < 3600:
            mins = int(diff / 60)
            return f"{mins}m ago"
        elif diff < 86400:
            hrs = int(diff / 3600)
            mins = int((diff % 3600) / 60)
            if mins > 0:
                return f"{hrs}h {mins}m ago"
            return f"{hrs}h ago"
        elif diff < 172800:
            # Yesterday
            dt_local = datetime.fromtimestamp(epoch)
            return f"yesterday at {dt_local.strftime('%-I:%M%p').lower()}"
        else:
            dt_local = datetime.fromtimestamp(epoch)
            return dt_local.strftime("%b %-d at %-I:%M%p").lower()
    except (ValueError, TypeError, OSError):
        return str(ts)


# ---------------------------------------------------------------------------
# Imp 3 — Task duration helper
# ---------------------------------------------------------------------------

def _task_duration(created_at, updated_at) -> str:
    """Calculate wall-clock duration from created_at to updated_at.
    
    Returns: 'completed in 23s', 'ran for 4m 12s', or '' if unavailable.
    """
    try:
        if not created_at or not updated_at:
            return ""
        
        def to_epoch(ts):
            if isinstance(ts, (int, float)):
                return float(ts)
            ts_clean = str(ts).replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(ts_clean).timestamp()
            except ValueError:
                return float(ts)
        
        c = to_epoch(created_at)
        u = to_epoch(updated_at)
        diff = max(0, u - c)
        
        if diff < 60:
            return f"completed in {int(diff)}s"
        elif diff < 3600:
            mins = int(diff / 60)
            secs = int(diff % 60)
            if secs > 0:
                return f"ran for {mins}m {secs}s"
            return f"ran for {mins}m"
        else:
            hrs = int(diff / 3600)
            mins = int((diff % 3600) / 60)
            return f"ran for {hrs}h {mins}m"
    except (ValueError, TypeError):
        return ""


# ---------------------------------------------------------------------------
# Fix 4 — Prompt logger helper
# ---------------------------------------------------------------------------

def _log_task(task_id: str, prompt: str) -> None:
    try:
        record = json.dumps({
            "task_id": task_id,
            "prompt": prompt,
            "created_at": int(time.time()),
        })
        with open(_TASK_LOG_PATH, "a") as f:
            f.write(record + "\n")
    except Exception as e:
        logger.warning("[task_log] Failed to write log: %s", e)


def _read_task_log(hours: int = 24) -> list:
    cutoff = int(time.time()) - (hours * 3600)
    entries = []
    try:
        with open(_TASK_LOG_PATH) as f:
            for line in f:
                try:
                    rec = json.loads(line.strip())
                    if rec.get("created_at", 0) >= cutoff:
                        entries.append(rec)
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return entries


# ---------------------------------------------------------------------------
# Imp 1 — LLM summarization helper for garza_status
# ---------------------------------------------------------------------------

async def _llm_summarize(query: str, digest_data: dict) -> str:
    """Call Gemini 2.5 Flash to generate a prose answer shaped to the query.
    
    Falls back gracefully to structured output if LLM is unavailable.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return ""  # Fall back to structured output
    
    try:
        import httpx
        
        tasks = digest_data.get("tasks", [])
        total = len(tasks)
        completed = sum(1 for t in tasks if t.get("status") == "completed")
        failed = sum(1 for t in tasks if t.get("status") == "failed")
        running = sum(1 for t in tasks if t.get("status") in ("running", "pending"))
        total_credits = digest_data.get("total_credits", 0)
        hours = digest_data.get("hours", 24)     
        task_lines = []
        for t in tasks[:8]:  # Limit context
            tid = t.get("id", "?")
            status = t.get("status", "?")
            prompt = t.get("prompt_preview", "(no prompt)")
            duration = t.get("duration", "")
            credits = t.get("credits", 0)
            line = f"- [{status.upper()}] {prompt[:120]}"
            if duration:
                line += f" ({duration})"
            if credits:
                line += f" — {_usd(credits)}"
            task_lines.append(line)
        
        warnings = digest_data.get("warnings", [])

        context = f"""Recent Manus AI activity (last {hours}h):
- Total tasks: {total}
- Completed: {completed}, Failed: {failed}, Running: {running}
- Cost: {_usd(total_credits)} ({total_credits} credits)
{chr(10).join(task_lines)}
{chr(10).join(warnings) if warnings else ''}"""
        
        system_prompt = """You are GARZA OS, Jaden Garza's AI estate manager. 
Answer questions about recent Manus AI activity in 2-4 natural sentences.
Be direct and conversational. Lead with the answer to the question asked.
Use specific numbers. Flag warnings prominently with ⚠️.
Never use bullet points. Write as if speaking to Jaden directly."""
        
        full_prompt = f"{system_prompt}\n\nQuery: {query}\n\nContext:\n{context}"

        # Call Gemini 2.5 Flash via REST API
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}",
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": full_prompt}]}],
                    "generationConfig": {
                        "maxOutputTokens": 300,
                        "temperature": 0.3,
                    },
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text", "").strip()
    except Exception as e:
        logger.warning("[garza_status] Gemini summarization failed: %s", e)
    
    return ""  # Fall back to structured output


# ---------------------------------------------------------------------------
# Synchronous tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def bash(command: str) -> str:
    """Execute a bash command and return its output."""
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode(errors="replace")


@mcp.tool()
async def editor(command: str, path: str, content: str = "") -> str:
    """Read or write a file. command: 'view' | 'create' | 'str_replace'."""
    if command == "view":
        try:
            return open(path).read()
        except FileNotFoundError:
            return f"File not found: {path}"
    elif command == "create":
        import pathlib
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return f"File written: {path}"
    return f"Unknown editor command: {command}"


@mcp.tool()
async def terminate(reason: str = "") -> str:
    """Signal task completion to the orchestrator."""
    return f"Task terminated. Reason: {reason}"


# ---------------------------------------------------------------------------
# Asynchronous tools (Manus SaaS API)
# ---------------------------------------------------------------------------

@mcp.tool()
async def manus_create_task(
    prompt: str,
    task_mode: str = "agent",
    agent_profile: str = "manus-1.6",
    file_ids: str = "",
    use_gmail_connector: bool = False,
    use_notion_connector: bool = False,
    use_gcal_connector: bool = False,
) -> str:
    """
    Submit a long-running task to Manus AI for autonomous execution.

    Returns a task_id — poll with manus_get_task until status == 'completed'.
    Tasks typically complete in 2-10 minutes.

    Args:
        prompt: Natural language task description (10-10,000 chars)
        task_mode: agent (default, full autonomous), adaptive, or chat
        agent_profile: manus-1.6 (default), manus-1.6-lite (fast/cheap), or manus-1.6-max (highest quality)
        file_ids: Comma-separated file IDs from manus_upload_file (optional)
        use_gmail_connector: Grant Manus access to Gmail
        use_notion_connector: Grant Manus access to Notion
        use_gcal_connector: Grant Manus access to Google Calendar
    """
    try:
        profile_map = {
            "speed": "manus-1.6", "quality": "manus-1.6-max",
            "lite": "manus-1.6-lite", "general": "manus-1.6", "default": "manus-1.6"
        }
        api_profile = profile_map.get(agent_profile, agent_profile)
        body: dict = {
            "prompt": prompt,
            "taskMode": task_mode,
            "agentProfile": api_profile,
        }

        if file_ids:
            body["attachments"] = [
                {"type": "file_id", "file_id": fid.strip()}
                for fid in file_ids.split(",") if fid.strip()
            ]

        connectors = []
        connector_map = {
            use_gmail_connector: "MANUS_GMAIL_CONNECTOR_ID",
            use_notion_connector: "MANUS_NOTION_CONNECTOR_ID",
            use_gcal_connector: "MANUS_GCAL_CONNECTOR_ID",
        }
        for enabled, env_var in connector_map.items():
            if enabled:
                cid = os.environ.get(env_var, "")
                if cid:
                    connectors.append({"id": cid})
                else:
                    logger.warning("[manus] %s env var not set — connector skipped", env_var)
        if connectors:
            body["connectors"] = connectors

        result = await manus_request("POST", "/tasks", json=body)
        task_id = result.get("id") or result.get("task_id", "unknown")
        status = result.get("status", "pending")
        created_at = result.get("created_at", "")

        # Fix 4 — Log prompt for NL query support
        _log_task(task_id, prompt)

        created_human = _human_time(created_at) if created_at else "just now"

        return (
            f"Task created successfully.\n"
            f"task_id : {task_id}\n"
            f"status  : {status}\n"
            f"created : {created_human}\n"
            f"prompt  : {prompt[:120]}{'...' if len(prompt) > 120 else ''}\n"
            f"Tip     : Call manus_get_task(task_id='{task_id}') in 2-3 minutes to check progress."
        )
    except Exception as e:
        return handle_api_error(e)


@mcp.tool()
async def manus_get_task(task_id: str) -> str:
    """
    Poll the status and output of a Manus task.
    Returns the original prompt, current status, duration, and Manus's result text.

    Args:
        task_id: The task ID returned by manus_create_task
    """
    try:
        result = await manus_request("GET", f"/tasks/{task_id}")
        status = result.get("status", "unknown")
        meta = result.get("metadata") or {}
        task_title = meta.get("task_title", "")
        task_url = meta.get("task_url", "")
        created_at = result.get("created_at", "")
        updated_at = result.get("updated_at", "")
        error_msg = result.get("error_message") or result.get("error") or ""

        # Fix 2 — Extract prompt and result from output array
        output_items = result.get("output") or []
        user_prompt = ""
        assistant_result = ""
        credit_usage = None

        if isinstance(output_items, list):
            for item in output_items:
                role = item.get("role", "")
                content_list = item.get("content", [])
                text_parts = []
                for c in content_list:
                    if isinstance(c, dict) and c.get("type") == "output_text":
                        text_parts.append(c.get("text", ""))
                text = " ".join(text_parts).strip()
                if role == "user" and not user_prompt and text:
                    user_prompt = text[:500]
                elif role == "assistant" and text:
                    assistant_result = text
                usage = item.get("usage") or {}
                if usage.get("total_tokens"):
                    credit_usage = usage.get("total_tokens")
        elif isinstance(output_items, str):
            assistant_result = output_items

        # Fall back to task log for prompt
        if not user_prompt:
            for entry in _read_task_log(hours=168):
                if entry.get("task_id") == task_id:
                    user_prompt = entry.get("prompt", "")[:500]
                    break

        # Imp 2 — Human-readable timestamps
        created_human = _human_time(created_at) if created_at else ""
        updated_human = _human_time(updated_at) if updated_at else ""

        # Imp 3 — Duration calculation
        duration = _task_duration(created_at, updated_at)

        lines = [
            f"task_id     : {task_id}",
            f"status      : {status}",
        ]
        if task_title:
            lines.append(f"title       : {task_title}")
        if task_url:
            lines.append(f"url         : {task_url}")
        if created_human:
            lines.append(f"created     : {created_human}")
        if updated_human and updated_human != created_human:
            lines.append(f"updated     : {updated_human}")
        if duration:
            lines.append(f"duration    : {duration}")
            if credit_usage is not None:
                lines.append(f"tokens_used : {credit_usage}")
        if user_prompt:
            lines.append(f"\nYour prompt :\n  {user_prompt}")
        if assistant_result:
            lines.append(f"\nManus result:\n{assistant_result[:2000]}")
            if len(assistant_result) > 2000:
                lines.append(f"  ... (truncated — {len(assistant_result)} chars total)")
        if error_msg:
            lines.append(f"\nError: {error_msg}")
        if status not in ("completed", "failed", "cancelled"):
            lines.append("\nTip: Task still in progress — check again in 1-2 minutes.")

        return "\n".join(lines)
    except Exception as e:
        return handle_api_error(e)


@mcp.tool()
async def manus_list_tasks(
    limit: int = 20,
    status_filter: str = "",
) -> str:
    """
    List recent Manus tasks with credit usage, timing, and prompt context.

    Args:
        limit: Number of tasks to return (1-100, default 20)
        status_filter: Optional filter — pending | running | completed | failed
    """
    try:
        params: dict = {"limit": limit}
        if status_filter:
            params["status"] = status_filter

        result = await manus_request("GET", "/tasks", params=params)
        tasks = result.get("tasks", result.get("data", []))
        has_more = result.get("has_more", False)

        if not tasks:
            return "No tasks found."

        log_by_id = {e["task_id"]: e["prompt"] for e in _read_task_log(hours=168)}

        lines = [f"Tasks ({len(tasks)} returned):"]
        for t in tasks:
            tid = t.get("id", t.get("task_id", "?"))
            tstatus = t.get("status", "?")
            created_at = t.get("created_at", "")
            updated_at = t.get("updated_at", "")
            credit_usage = t.get("credit_usage") or t.get("credits_used")
            meta = t.get("metadata") or {}
            title = meta.get("task_title") or (t.get("prompt", "") or "")[:60]
            url = meta.get("task_url", "")

            # Imp 2 — Human-readable timestamps
            created_human = _human_time(created_at) if created_at else ""
            # Imp 3 — Duration
            duration = _task_duration(created_at, updated_at)

            prompt_preview = log_by_id.get(tid, "")[:80]

            line = f"  {tid}  [{tstatus}]"
            if created_human:
                line += f"  {created_human}"
            if duration:
                line += f"  ({duration})"
            if credit_usage is not None:
                line += f"  {_usd(credit_usage)}"
            line += f"  {title!r}"
            if prompt_preview and prompt_preview not in title:
                line += f"  prompt:'{prompt_preview}'"
            if url:
                line += f"  {url}"
            lines.append(line)

        if has_more:
            lines.append("\nMore tasks available — reduce limit or use status_filter.")

        return "\n".join(lines)
    except Exception as e:
        return handle_api_error(e)


@mcp.tool()
async def manus_upload_file(filename: str, content_base64: str, content_type: str = "text/plain") -> str:
    """
    Upload a file to Manus and get a file_id for use in manus_create_task.

    Args:
        filename: Name of the file (e.g., 'report.pdf')
        content_base64: Base64-encoded file content
        content_type: MIME type (default text/plain)
    """
    try:
        import base64
        import httpx

        presign = await manus_request("POST", "/files", json={
            "filename": filename,
            "content_type": content_type,
        })
        upload_url = presign.get("upload_url")
        file_id = presign.get("id") or presign.get("file_id")

        if not upload_url or not file_id:
            return f"Error: Unexpected presign response — {presign}"

        raw_bytes = base64.b64decode(content_base64)
        async with httpx.AsyncClient(timeout=60.0) as client:
            put_resp = await client.put(
                upload_url,
                content=raw_bytes,
                headers={"Content-Type": content_type},
            )
            put_resp.raise_for_status()

        return (
            f"File uploaded successfully.\n"
            f"file_id  : {file_id}\n"
            f"filename : {filename}\n"
            f"Tip      : Pass file_ids='{file_id}' when calling manus_create_task."
        )
    except Exception as e:
        return handle_api_error(e)


@mcp.tool()
async def manus_list_files(limit: int = 20) -> str:
    """
    List files uploaded to Manus.

    Args:
        limit: Number of files to return (default 20)
    """
    try:
        result = await manus_request("GET", "/files", params={"limit": limit})
        files = result.get("files", result.get("data", []))

        if not files:
            return "No files found."

        lines = [f"Files ({len(files)}):"]
        for f in files:
            fid = f.get("id", "?")
            name = f.get("filename") or f.get("name", "?")
            size = f.get("size", "?")
            created_at = f.get("created_at", "")
            created_human = _human_time(created_at) if created_at else ""
            lines.append(f"  {fid}  {name}  {size} bytes  {created_human}")

        return "\n".join(lines)
    except Exception as e:
        return handle_api_error(e)


@mcp.tool()
async def manus_create_webhook(
    url: str,
    events: str = "task.completed,task.failed",
) -> str:
    """
    Register a webhook to receive Manus task completion events.

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
            f"Webhook registered.\n"
            f"webhook_id : {webhook_id}\n"
            f"url        : {url}\n"
            f"events     : {event_list}\n"
            f"secret     : {secret}\n"
            f"Tip        : Store the secret in Railway env as MANUS_WEBHOOK_SECRET "
            f"and verify it in your n8n workflow."
        )
    except Exception as e:
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# garza_status — Sprint 2: LLM summarization + runaway task warning
# ---------------------------------------------------------------------------

_RUNAWAY_MINUTES = int(os.environ.get("GARZA_RUNAWAY_MINUTES", "30"))
_RUNAWAY_CREDITS = int(os.environ.get("GARZA_RUNAWAY_CREDITS", "10"))


@mcp.tool()
async def garza_status(
    query: str = "What did Manus do today?",
    hours: int = 24,
    limit: int = 10,
) -> str:
    """
    Answer natural language questions about recent Manus activity.
    Returns a prose answer shaped to the question asked, powered by LLM summarization.

    Examples:
      - "What did Manus accomplish today?"
      - "What was the last thing I asked Manus?"
      - "Did anything fail in the last 6 hours?"
      - "How many credits did I spend today?"
      - "Is there anything still running?"

    Args:
        query: Plain-English question about recent Manus activity
        hours: How many hours back to look (default 24)
        limit: Max tasks to include in the digest (default 10)
    """
    try:
        result = await manus_request("GET", "/tasks", params={"limit": limit})
        tasks = result.get("tasks", result.get("data", []))

        log_by_id = {e["task_id"]: e for e in _read_task_log(hours=hours)}

        cutoff = int(time.time()) - (hours * 3600)
        recent_tasks = []
        for t in tasks:
            created_raw = t.get("created_at", "0")
            try:
                created_ts = int(created_raw)
            except (ValueError, TypeError):
                created_ts = 0
            if created_ts >= cutoff or created_ts == 0:
                recent_tasks.append(t)

        if not recent_tasks:
            return f"No Manus tasks found in the last {hours} hours."

        total_credits = 0
        statuses = {}
        task_summaries = []
        warnings = []
        enriched_tasks = []

        now = time.time()

        for t in recent_tasks:
            tid = t.get("id", "?")
            tstatus = t.get("status", "?")
            statuses[tstatus] = statuses.get(tstatus, 0) + 1
            credits = t.get("credit_usage") or t.get("credits_used") or 0
            try:
                credits_int = int(credits)
                total_credits += credits_int
            except (TypeError, ValueError):
                credits_int = 0

            meta = t.get("metadata") or {}
            title = meta.get("task_title", "")
            url = meta.get("task_url", "")
            created_at = t.get("created_at", "")
            updated_at = t.get("updated_at", "")

            # Imp 2 — Human-readable timestamps
            created_human = _human_time(created_at) if created_at else ""
            # Imp 3 — Duration
            duration = _task_duration(created_at, updated_at)

            # Imp 4 — Runaway task detection
            if tstatus in ("running", "pending") and created_at:
                try:
                    elapsed_mins = (now - float(created_at)) / 60
                    if elapsed_mins > _RUNAWAY_MINUTES:
                        warnings.append(
                            f"⚠️  RUNAWAY TASK: {tid} has been running for "
                            f"{int(elapsed_mins)}m (>{_RUNAWAY_MINUTES}m threshold)"
                        )
                    elif credits_int > _RUNAWAY_CREDITS:
                        warnings.append(
                            f"⚠️  HIGH CREDIT USAGE: {tid} consumed {_usd(credits_int)} ({credits_int} credits) "
                            f"while still running (>{_RUNAWAY_CREDITS} threshold)"
                        )
                except (ValueError, TypeError):
                    pass

            log_entry = log_by_id.get(tid, {})
            prompt = log_entry.get("prompt", title or "(no prompt recorded)")[:200]

            output_items = t.get("output") or []
            result_preview = ""
            if isinstance(output_items, list):
                for item in reversed(output_items):
                    if item.get("role") == "assistant":
                        for c in item.get("content", []):
                            if isinstance(c, dict) and c.get("type") == "output_text":
                                result_preview = c.get("text", "")[:300]
                                break
                    if result_preview:
                        break

            enriched_tasks.append({
                "id": tid,
                "status": tstatus,
                "prompt_preview": prompt,
                "duration": duration,
                "credits": credits_int,
                "created_human": created_human,
                "url": url,
                "result_preview": result_preview,
            })

            summary = f"  [{tstatus.upper()}] {tid}"
            if created_human:
                summary += f"  ({created_human})"
            if duration:
                summary += f"  {duration}"
            if credits_int:
                summary += f"  {_usd(credits_int)}"
            summary += f"\n    Asked: {prompt}"
            if result_preview:
                summary += f"\n    Result: {result_preview}"
            if url:
                summary += f"\n    Link: {url}"
            task_summaries.append(summary)

        # Imp 1 — Try LLM summarization first
        digest_data = {
            "tasks": enriched_tasks,
            "total_credits": total_credits,
            "hours": hours,
            "warnings": warnings,
        }
        prose_answer = await _llm_summarize(query, digest_data)

        if prose_answer:
            # LLM answer available — lead with prose, then append full structured digest
            lines = [f"GARZA OS — Manus Activity Digest (last {hours}h)"]
            lines.append(f"Query: \"{query}\"\n")
            lines.append(prose_answer)
            if warnings:
                lines.append("")
                lines.extend(warnings)
            # Always append the full task details so nothing is truncated
            lines.append("")
            lines.append("Summary:")
            lines.append(f"  Tasks found : {len(recent_tasks)}")
            for s, count in sorted(statuses.items()):
                lines.append(f"  {s:12s}: {count}")
            if total_credits > 0:
                lines.append(f"  Cost: {_usd(total_credits)} ({total_credits} credits)")
            lines.append("")
            lines.append("Task Details:")
            lines.extend(task_summaries)
            return "\n".join(lines)

        # Fallback — structured output (same as before but with human timestamps + duration)
        lines = [
            f"GARZA OS — Manus Activity Digest (last {hours}h)",
            f"Query: \"{query}\"",
            f"",
            f"Summary:",
            f"  Tasks found : {len(recent_tasks)}",
        ]
        for s, count in sorted(statuses.items()):
            lines.append(f"  {s:12s}: {count}")
        if total_credits > 0:
            lines.append(f"  Cost: {_usd(total_credits)} ({total_credits} credits)")

        if warnings:
            lines.append("")
            lines.extend(warnings)

        lines.append("")
        lines.append("Task Details:")
        lines.extend(task_summaries)

        # Query-specific answers
        q_lower = query.lower()
        if any(w in q_lower for w in ["fail", "error", "broke", "wrong"]):
            failed = [t for t in recent_tasks if t.get("status") == "failed"]
            if not failed:
                lines.append("\n✅ No failures in this period.")
            else:
                lines.append(f"\n🔴 {len(failed)} task(s) failed.")
        if any(w in q_lower for w in ["running", "progress", "pending", "still"]):
            active = [t for t in recent_tasks if t.get("status") in ("running", "pending")]
            if not active:
                lines.append("\n✅ No tasks currently running.")
            else:
                lines.append(f"\n🟡 {len(active)} task(s) still active.")
        if any(w in q_lower for w in ["last", "recent", "latest"]):
            if recent_tasks:
                last = recent_tasks[0]
                lid = last.get("id", "?")
                log_entry = log_by_id.get(lid, {})
                last_prompt = log_entry.get("prompt", "(no prompt recorded)")
                lines.append(f"\n📌 Last task: {last_prompt[:200]}")

        return "\n".join(lines)
    except Exception as e:
        return handle_api_error(e)
