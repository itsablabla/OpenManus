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
    agent_name: str = "",
    agent_purpose: str = "",
    agent_tags: str = "",
    triggered_by: str = "jaden",
) -> str:
    """
    Submit a long-running task to Manus AI for autonomous execution.

    Returns a task_id — poll with manus_get_task until status == 'completed'.
    Tasks typically complete in 2-10 minutes.

    Args:
        prompt: Natural language task description (10-10,000 chars)
        task_mode: agent (default, full autonomous), adaptive (balanced), or chat (conversational). 'auto' is an alias for 'adaptive'.
        agent_profile: manus-1.6 (default), manus-1.6-lite (fast/cheap), or manus-1.6-max (highest quality)
        file_ids: Comma-separated file IDs from manus_upload_file (optional)
        use_gmail_connector: Grant Manus access to Gmail
        use_notion_connector: Grant Manus access to Notion
        use_gcal_connector: Grant Manus access to Google Calendar
        agent_name: Optional human-readable name for this agent (registered in agent registry)
        agent_purpose: Optional one-sentence purpose description
        agent_tags: Optional comma-separated tags (e.g. 'build,research,memory')
        triggered_by: Who triggered this agent (jaden | manus | n8n | auto)
    """
    try:
        profile_map = {
            "speed": "manus-1.6", "quality": "manus-1.6-max",
            "lite": "manus-1.6-lite", "general": "manus-1.6", "default": "manus-1.6"
        }
        api_profile = profile_map.get(agent_profile, agent_profile)
        # Normalize task_mode aliases — 'auto' is not a valid Manus API value
        mode_map = {"auto": "adaptive", "autonomous": "agent", "full": "agent", "fast": "adaptive"}
        api_task_mode = mode_map.get(task_mode.lower(), task_mode)
        body: dict = {
            "prompt": prompt,
            "taskMode": api_task_mode,
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

        # Phase 2 — Inject relevant memories into the prompt (graceful degradation)
        enriched_prompt = prompt
        if _FABRIC_SO_URL:
            try:
                mem_result = await fabric_call("agent_session_start", {"task_description": prompt[:300]})
                context_block = mem_result.get("context_block", mem_result.get("context", ""))
                if context_block:
                    enriched_prompt = f"{context_block}\n\n---\n\nTask: {prompt}"
                    logger.info("[memory] Injected %d chars of context into task prompt", len(context_block))
            except Exception as e:
                logger.warning("[memory] Failed to load session context: %s", e)

        body["prompt"] = enriched_prompt

        result = await manus_request("POST", "/tasks", json=body)
        task_id = result.get("id") or result.get("task_id", "unknown")
        status = result.get("status", "pending")
        created_at = result.get("created_at", "")

        # Fix 4 — Log prompt for NL query support
        _log_task(task_id, prompt)

        # Phase 2 — Store task as a Context memory (graceful degradation)
        if _FABRIC_SO_URL:
            try:
                await fabric_call("agent_remember_context", {
                    "text": f"Started Manus task {task_id}: {prompt[:200]}"
                })
            except Exception as e:
                logger.warning("[memory] Failed to store task context: %s", e)

        created_human = _human_time(created_at) if created_at else "just now"
        # Register agent in registry (always — auto-generates name from prompt if not provided)
        registry_note = ""
        try:
            tags_list = [t.strip() for t in agent_tags.split(",") if t.strip()] if agent_tags else []
            _registry_add(
                task_id=task_id, prompt=prompt, agent_name=agent_name,
                agent_purpose=agent_purpose, triggered_by=triggered_by, tags=tags_list,
            )
            reg_name = agent_name or _auto_agent_name(prompt)
            registry_note = f"\nregistered: {reg_name} (use agent_registry_list to view)"
        except Exception as reg_err:
            logger.warning("[registry] Failed to register agent: %s", reg_err)
        return (
            f"Task created successfully.\n"
            f"task_id : {task_id}\n"
            f"status  : {status}\n"
            f"created : {created_human}\n"
            f"prompt  : {prompt[:120]}{'...' if len(prompt) > 120 else ''}\n"
            f"Tip     : Call manus_get_task(task_id='{task_id}') in 2-3 minutes to check progress."
            f"{registry_note}"
        )
    except Exception as e:
        error_msg = str(e)
        logger.error("[manus_create_task] Task creation failed: %s", error_msg)
        return f"ERROR: Task creation failed.\nReason: {error_msg}\nCheck your Manus API key and prompt."


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
    limit: int = 50,
    status_filter: str = "",
    paginate: bool = True,
) -> str:
    """
    List recent Manus tasks with credit usage, timing, and prompt context.
    Paginates automatically to reach the requested limit (max 500).

    Args:
        limit: Number of tasks to return (default 50, max 500)
        status_filter: Optional filter — pending | running | completed | failed
        paginate: If True (default), fetch multiple pages to reach limit
    """
    try:
        effective_limit = min(max(1, limit), 500)
        tasks = []
        page = 1
        while len(tasks) < effective_limit:
            batch_size = min(100, effective_limit - len(tasks))
            params: dict = {"limit": batch_size, "page": page}
            if status_filter:
                params["status"] = status_filter
            result = await manus_request("GET", "/tasks", params=params)
            batch = result.get("tasks", result.get("data", []))
            if not batch:
                break
            tasks.extend(batch)
            has_more = result.get("has_more", False)
            if not paginate or not has_more or len(batch) < batch_size:
                break
            page += 1
            if page > 10:
                break

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
# Phase 2 — fabric-so-mcp Memory Integration
# ---------------------------------------------------------------------------

_FABRIC_SO_URL = os.environ.get("FABRIC_SO_MCP_URL", "")
_FABRIC_SO_API_KEY = os.environ.get("FABRIC_SO_API_KEY", "")


async def fabric_call(tool_name: str, params: dict) -> dict:
    """Call a tool on the fabric-so-mcp server. Returns {} on failure (graceful degradation)."""
    if not _FABRIC_SO_URL or not _FABRIC_SO_API_KEY:
        return {}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_FABRIC_SO_URL.rstrip('/')}/tools/{tool_name}",
                headers={
                    "Authorization": f"Bearer {_FABRIC_SO_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=params,
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning("[fabric_call] %s returned %s: %s", tool_name, resp.status_code, resp.text[:200])
    except Exception as e:
        logger.warning("[fabric_call] %s failed: %s", tool_name, e)
    return {}


@mcp.tool()
async def garza_recall(query: str, limit: int = 5) -> str:
    """
    Search GARZA OS long-term memory for context across all past sessions.
    Powered by fabric-so-mcp AutoMem memory layer.

    Use this to answer:
      - 'When did I last research Verizon billing?'
      - 'What decisions have been made about the MCP server?'
      - 'What does Jaden prefer for briefing format?'
      - 'What has Manus learned about my preferences?'

    Args:
        query: Natural language question to search memory
        limit: Max memories to return (default 5)
    """
    if not _FABRIC_SO_URL:
        return (
            "Memory layer not configured. "
            "Set FABRIC_SO_MCP_URL and FABRIC_SO_API_KEY in Railway environment variables "
            "to enable cross-session memory recall."
        )
    try:
        results = await fabric_call("agent_recall", {"query": query, "limit": limit})
        memories = results.get("memories", results.get("results", []))

        if not memories:
            return f"No memories found matching: '{query}'"

        lines = [f"Found {len(memories)} memories for: '{query}'"]
        for mem in memories:
            mem_type = mem.get("type", mem.get("memory_type", "unknown"))
            text = mem.get("text", mem.get("content", ""))
            created = mem.get("created_at", "")
            created_human = _human_time(created) if created else ""
            importance = mem.get("importance", "")

            line = f"  [{mem_type.upper()}] {text}"
            if created_human:
                line += f" ({created_human})"
            if importance:
                line += f" [importance: {importance}]"
            lines.append(line)

        return "\n".join(lines)
    except Exception as e:
        return f"Error recalling from memory: {e}"


# ---------------------------------------------------------------------------
# Phase 1 — Agent Observability Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def manus_get_steps(task_id: str) -> str:
    """
    Get the step-by-step trace of what Manus did inside a task.
    Shows each tool used, action taken, duration, and result summary.

    Use this to answer: 'What did Manus actually do for 38 minutes?'
    or 'Which step timed out?' or 'Where did the credits go?'

    Args:
        task_id: The task ID to inspect
    """
    try:
        # The Manus API does not expose a /steps endpoint.
        # The conversation turns (output array) in the task detail ARE the step trace.
        detail = await manus_request("GET", f"/tasks/{task_id}")
        output = detail.get("output", [])
        meta = detail.get("metadata") or {}
        title = meta.get("task_title", "(no title)")
        status = detail.get("status", "unknown")
        credits = detail.get("credit_usage", 0)
        task_url = meta.get("task_url", "")

        if not output:
            return (
                f"No step trace available for task {task_id}.\n"
                f"Status : {status}\n"
                f"Title  : {title}\n"
                f"Note   : Task has no output turns yet."
            )

        # Extract meaningful turns (skip empty assistant stubs)
        turns = []
        for item in output:
            role = item.get("role", "")
            content = item.get("content", [])
            text = ""
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "output_text":
                        text = c.get("text", "")[:300]
                        break
            elif isinstance(content, str):
                text = content[:300]
            if text:
                turns.append((role, text))

        lines = [
            f"Step trace for task {task_id} ({len(turns)} turns):",
            f"  Title  : {title}",
            f"  Status : {status}  |  Cost: {_usd(credits)}",
        ]
        if task_url:
            lines.append(f"  URL    : {task_url}")
        lines.append("")

        for i, (role, text) in enumerate(turns):
            prefix = "\U0001f464 User" if role == "user" else "\U0001f916 Manus"
            lines.append(f"  [{i+1}] {prefix}: {text}")

        lines.append(f"\nTotal: {len(turns)} turns, {_usd(credits)} spent")
        return "\n".join(lines)
    except Exception as e:
        return handle_api_error(e)


@mcp.tool()
async def manus_get_parent(task_id: str) -> str:
    """
    Resolve the parent task for a given subtask ID.
    Use this to find out which parent flow spawned a 'Wide Research Subtask'.

    Args:
        task_id: The subtask ID to resolve
    """
    try:
        # The Manus API does not expose a parent_id field in the task object.
        # We return the task's own metadata and note the API limitation.
        detail = await manus_request("GET", f"/tasks/{task_id}")
        meta = detail.get("metadata") or {}
        title = meta.get("task_title", "(no title)")
        status = detail.get("status", "unknown")
        credits = detail.get("credit_usage", 0)
        task_url = meta.get("task_url", "")
        created = detail.get("created_at", "")
        human_time = _human_time(created) if created else ""

        # Detect if this looks like a subtask by title pattern
        subtask_patterns = ["subtask", "sub-task", "wide research", "parallel", "worker"]
        looks_like_subtask = any(p in title.lower() for p in subtask_patterns)

        lines = [
            f"Task info for {task_id}:",
            f"  title   : {title}",
            f"  status  : {status}  |  cost: {_usd(credits)}",
        ]
        if human_time:
            lines.append(f"  created : {human_time}")
        if task_url:
            lines.append(f"  url     : {task_url}")
        lines.append("")

        if looks_like_subtask:
            lines.append(
                "\u26a0\ufe0f  This task title suggests it may be a subtask, but the Manus API "
                "does not expose a parent_id field. To find the parent, use manus_list_tasks "
                "and look for a task created around the same time with a broader title."
            )
        else:
            lines.append(
                "\u2139\ufe0f  The Manus API does not expose parent/child task relationships. "
                "This appears to be a top-level task based on its title."
            )

        return "\n".join(lines)
    except Exception as e:
        return handle_api_error(e)


@mcp.tool()
async def manus_watch_task(
    task_id: str,
    poll_interval_seconds: int = 30,
    max_polls: int = 60,
) -> str:
    """
    Monitor a running Manus task and stream status updates until completion.
    Returns a full timeline of status changes and the final result.

    Use this for passive awareness — you get told when tasks finish.
    Fires a ⚠️ warning if credits exceed $5 while still running.

    Args:
        task_id: The task ID to watch
        poll_interval_seconds: How often to poll (default 30s)
        max_polls: Maximum number of polls before giving up (default 60 = 30 min)
    """
    try:
        updates = []
        start_time = time.time()
        last_status = None
        credit_warned = False

        for attempt in range(max_polls):
            if attempt > 0:
                await asyncio.sleep(poll_interval_seconds)

            result = await manus_request("GET", f"/tasks/{task_id}")
            status = result.get("status", "unknown")
            credits = result.get("credit_usage") or result.get("credits_used") or 0
            try:
                credits_int = int(credits)
            except (TypeError, ValueError):
                credits_int = 0

            elapsed = int(time.time() - start_time)
            elapsed_str = f"{elapsed // 60}m {elapsed % 60}s" if elapsed >= 60 else f"{elapsed}s"

            if status != last_status:
                updates.append(f"  [{elapsed_str}] Status changed: {last_status or 'start'} → {status}")
                last_status = status

            # Credit runaway warning
            if credits_int > 1000 and not credit_warned and status in ("running", "pending"):
                updates.append(f"  ⚠️  [{elapsed_str}] HIGH COST WARNING: {_usd(credits_int)} spent while still running")
                credit_warned = True

            if status in ("completed", "failed", "cancelled"):
                meta = result.get("metadata") or {}
                title = meta.get("task_title", "")
                url = meta.get("task_url", "")

                output_items = result.get("output") or []
                result_preview = ""
                if isinstance(output_items, list):
                    for item in reversed(output_items):
                        if item.get("role") == "assistant":
                            for c in item.get("content", []):
                                if isinstance(c, dict) and c.get("type") == "output_text":
                                    result_preview = c.get("text", "")[:500]
                                    break
                        if result_preview:
                            break

                lines = [
                    f"Task {task_id} — WATCH COMPLETE",
                    f"Final status : {status}",
                    f"Total time   : {elapsed_str}",
                    f"Total cost   : {_usd(credits_int)} ({credits_int} credits)",
                ]
                if title:
                    lines.append(f"Title        : {title}")
                if url:
                    lines.append(f"URL          : {url}")
                lines.append("\nTimeline:")
                lines.extend(updates)
                if result_preview:
                    lines.append(f"\nResult preview:\n{result_preview}")
                return "\n".join(lines)

        # Timed out
        return (
            f"Task {task_id} — WATCH TIMEOUT\n"
            f"Polled {max_polls} times over {max_polls * poll_interval_seconds // 60}m — task still {last_status}.\n"
            f"Timeline:\n" + "\n".join(updates)
        )
    except Exception as e:
        return handle_api_error(e)


@mcp.tool()
async def manus_cost_summary(
    hours_back: int = 168,
    group_by: str = "day",
    days_back: int = 0,
) -> str:
    """
    Get a cost breakdown of Manus usage over any time range.
    Shows daily/weekly totals and the top 5 most expensive tasks.

    Use this to answer: 'How much have I spent this week?'
    or 'What are my most expensive tasks?'

    Args:
        hours_back: How many hours to look back (default 168 = 7 days)
        group_by: 'day' or 'week' (default 'day')
        days_back: Convenience alias — if set, overrides hours_back (days_back=1 = yesterday)
    """
    try:
        from collections import defaultdict
        if days_back > 0:
            hours_back = days_back * 24

        result = await manus_request("GET", "/tasks", params={"limit": 100})
        tasks = result.get("tasks", result.get("data", []))

        cutoff = time.time() - (hours_back * 3600)
        relevant = []
        for t in tasks:
            created_raw = t.get("created_at", "0")
            try:
                created_ts = float(created_raw)
            except (ValueError, TypeError):
                created_ts = 0
            if created_ts >= cutoff:
                relevant.append(t)

        if not relevant:
            return f"No tasks found in the last {hours_back} hours."

        by_period: dict = defaultdict(lambda: {"cost": 0.0, "credits": 0, "count": 0, "tasks": []})
        total_credits = 0

        for t in relevant:
            created_raw = t.get("created_at", "0")
            try:
                created_ts = float(created_raw)
                dt = datetime.fromtimestamp(created_ts)
                if group_by == "week":
                    period = dt.strftime("Week of %b %-d")
                else:
                    period = dt.strftime("%Y-%m-%d (%a)")
            except (ValueError, TypeError):
                period = "Unknown"

            credits = t.get("credit_usage") or t.get("credits_used") or 0
            try:
                credits_int = int(credits)
            except (TypeError, ValueError):
                credits_int = 0

            total_credits += credits_int
            by_period[period]["credits"] += credits_int
            by_period[period]["cost"] += credits_int / _CREDITS_PER_DOLLAR
            by_period[period]["count"] += 1

            meta = t.get("metadata") or {}
            title = meta.get("task_title", "(no title)")
            tid = t.get("id", "?")
            url = meta.get("task_url", "")
            by_period[period]["tasks"].append((credits_int, tid, title, url))

        lines = [
            f"Manus Cost Summary — last {hours_back}h ({len(relevant)} tasks)",
            f"Total: {_usd(total_credits)} ({total_credits} credits)",
            "",
            f"Breakdown by {group_by}:",
        ]

        for period in sorted(by_period.keys(), reverse=True):
            data = by_period[period]
            lines.append(
                f"  {period}: {_usd(data['credits'])} "
                f"({data['credits']} credits, {data['count']} tasks)"
            )

        # Top 5 most expensive tasks
        all_tasks_sorted = sorted(
            [(credits, tid, title, url)
             for period_data in by_period.values()
             for credits, tid, title, url in period_data["tasks"]],
            reverse=True,
        )[:5]

        if all_tasks_sorted:
            lines.append("")
            lines.append("Top 5 most expensive tasks:")
            for i, (credits, tid, title, url) in enumerate(all_tasks_sorted, 1):
                line = f"  {i}. {_usd(credits)} — {title[:60]!r} ({tid})"
                if url:
                    line += f"\n     {url}"
                lines.append(line)

        return "\n".join(lines)
    except Exception as e:
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# garza_status — Sprint 2: LLM summarization + runaway task warning
# ---------------------------------------------------------------------------

def _safe_int(val, default):
    """Parse int from env var, falling back to default if value is non-numeric."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

_RUNAWAY_MINUTES = _safe_int(os.environ.get("GARZA_RUNAWAY_MINUTES"), 30)
_RUNAWAY_CREDITS = _safe_int(os.environ.get("GARZA_RUNAWAY_CREDITS"), 10)


@mcp.tool()
async def garza_status(
    query: str = "What did Manus do today?",
    hours: int = 48,
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


# ===========================================================================
# GARZA OS Agent Intelligence Layer — Sprint 3 (2026-03-07)
# Tool 1: manus_diagnose_task  — LLM-powered step trace analysis
# Tool 2: manus_resume_task    — auto-unblock + enriched re-prompt
# Tool 3: garza_flow_status    — multi-task workflow grouping view
# ===========================================================================

_DIAGNOSE_SYSTEM = """You are GARZA OS Diagnostics, an expert at analyzing Manus AI agent task traces.
Given a step-by-step trace of what a Manus agent did, you must return a JSON object ONLY — no markdown, no explanation.

The JSON must have exactly these fields:
{
  "status": "completed_clean" | "completed_with_issues" | "stalled" | "failed" | "auth_blocked" | "confused",
  "blocked_at_step": "step number and short description, or null",
  "blocker_type": "auth_missing" | "api_error" | "missing_context" | "tool_not_found" | "timeout" | "none",
  "blocker_detail": "plain English explanation of exactly what was missing or wrong",
  "suggested_fix": "what information or action would unblock it",
  "resume_prompt": "a ready-to-use prompt string to feed back to Manus to continue with the right context injected"
}

Rules:
- If the task completed successfully with no errors, use status=completed_clean and blocker_type=none
- If you see auth errors (401, 403, API key issues), use status=auth_blocked and blocker_type=auth_missing
- If the agent repeated the same action 3+ times without progress, use status=stalled
- The resume_prompt must be specific and actionable — include the original goal plus any missing context
- Return ONLY the JSON object, nothing else"""


async def _diagnose_task_llm(task_id: str, step_trace: str, task_prompt: str) -> dict:
    """Call Gemini to analyze a step trace and return a structured diagnosis."""
    import requests as _req

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        return {
            "status": "unknown",
            "blocked_at_step": None,
            "blocker_type": "none",
            "blocker_detail": "GEMINI_API_KEY not configured — cannot perform LLM diagnosis",
            "suggested_fix": "Set GEMINI_API_KEY in Railway environment variables",
            "resume_prompt": task_prompt,
        }

    user_content = f"""Task ID: {task_id}
Original prompt: {task_prompt[:500]}

Step trace ({len(step_trace)} chars):
{step_trace[:8000]}

Analyze this trace and return the JSON diagnosis."""

    payload = {
        "system_instruction": {"parts": [{"text": _DIAGNOSE_SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": user_content}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048, "response_mime_type": "application/json"},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
    try:
        resp = _req.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        raw = resp.json()
        text = raw["candidates"][0]["content"]["parts"][0]["text"].strip()
        # Strip markdown code fences if present (regex handles edge cases)
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        return json.loads(text.strip())
    except Exception as exc:
        return {
            "status": "unknown",
            "blocked_at_step": None,
            "blocker_type": "none",
            "blocker_detail": f"LLM diagnosis failed: {exc}",
            "suggested_fix": "Check GEMINI_API_KEY and retry",
            "resume_prompt": task_prompt,
        }


@mcp.tool()
async def manus_diagnose_task(task_id: str) -> str:
    """Diagnose a Manus task — analyze its step trace with an LLM and return a structured diagnosis.

    Fetches the full step trace for the given task, passes it to Gemini 2.5 Flash,
    and returns a JSON diagnosis with:
      - status: completed_clean | completed_with_issues | stalled | failed | auth_blocked | confused
      - blocked_at_step: step number and description where it got stuck (if applicable)
      - blocker_type: auth_missing | api_error | missing_context | tool_not_found | timeout | none
      - blocker_detail: plain English explanation of what was missing or wrong
      - suggested_fix: what information or action would unblock it
      - resume_prompt: ready-to-use prompt to feed back to Manus to continue the task

    Args:
        task_id: The Manus task ID to diagnose (e.g. 'ttCBV4aBMeU5k5U2GoekFh')
    """
    try:
        # 1. Fetch task details to get the original prompt
        task_resp = await manus_request("GET", f"/tasks/{task_id}")
        task_data = task_resp if isinstance(task_resp, dict) else {}
        output = task_data.get("output", [])
        status = task_data.get("status", "unknown")
        credits_used = task_data.get("credit_usage", {})
        if isinstance(credits_used, dict):
            credits_int = credits_used.get("credits", 0)
        else:
            credits_int = int(credits_used) if credits_used else 0

        # Extract original prompt from first user message
        task_prompt = "(unknown)"
        for turn in output:
            if turn.get("role") == "user":
                content = turn.get("content", [])
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") in ("text", "output_text"):
                            task_prompt = part.get("text", "(unknown)")[:500]
                            break
                elif isinstance(content, str):
                    task_prompt = content[:500]
                break

        # 2. Build step trace from output array
        step_lines = []
        for i, turn in enumerate(output):
            role = turn.get("role", "?")
            content = turn.get("content", [])
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        ptype = part.get("type", "")
                        # Manus API uses output_text / output_file (not bare "text")
                        if ptype in ("text", "output_text"):
                            txt = part.get("text", "")[:300]
                            step_lines.append(f"Step {i+1} [{role}/text]: {txt}")
                        elif ptype in ("tool_use", "output_tool_use"):
                            tname = part.get("name", "?")
                            tinput = str(part.get("input", ""))[:200]
                            step_lines.append(f"Step {i+1} [{role}/tool_use]: {tname}({tinput})")
                        elif ptype in ("tool_result", "output_tool_result"):
                            tresult = str(part.get("content", ""))[:200]
                            step_lines.append(f"Step {i+1} [{role}/tool_result]: {tresult}")
                        elif ptype == "output_file":
                            fname = part.get("fileUrl", "").split("/")[-1].split("?")[0][:100]
                            step_lines.append(f"Step {i+1} [{role}/file]: {fname}")
            elif isinstance(content, str):
                step_lines.append(f"Step {i+1} [{role}]: {content[:300]}")

        step_trace = "\n".join(step_lines) if step_lines else f"No step trace available. Task status: {status}"

        # 3. Call LLM for diagnosis
        diagnosis = await _diagnose_task_llm(task_id, step_trace, task_prompt)

        # 4. Format output
        lines = [
            f"GARZA OS — Task Diagnosis: {task_id}",
            f"Status: {status} | Cost: {_usd(credits_int)} | Steps: {len(output)}",
            "",
            "=== LLM Diagnosis ===",
            json.dumps(diagnosis, indent=2),
            "",
            "=== Raw Step Count ===",
            f"{len(step_lines)} step turns analyzed",
            f"Original prompt: {task_prompt[:200]}",
            f"Task URL: https://manus.im/app/{task_id}",
        ]
        return "\n".join(lines)
    except Exception as e:
        return handle_api_error(e)


@mcp.tool()
async def manus_resume_task(task_id: str) -> str:
    """Auto-diagnose a stuck/failed task and create an enriched resume task.

    Steps:
      1. Calls manus_diagnose_task to get blocker type and suggested fix
      2. Calls garza_recall with a query derived from blocker_detail to search memory
      3. Combines: original task prompt + diagnosis + any memory context found
      4. Calls manus_create_task with an enriched prompt that includes all context
      5. Returns: diagnosis summary, memory found (if any), and the new task ID

    Args:
        task_id: The Manus task ID to resume (e.g. 'ttCBV4aBMeU5k5U2GoekFh')
    """
    try:
        # Step 1: Diagnose the task
        diagnosis_text = await manus_diagnose_task(task_id)

        # Extract the JSON diagnosis from the output
        diagnosis = {}
        try:
            # Find the JSON block in the diagnosis output
            start = diagnosis_text.find("{")
            end = diagnosis_text.rfind("}") + 1
            if start >= 0 and end > start:
                diagnosis = json.loads(diagnosis_text[start:end])
        except Exception:
            pass

        blocker_detail = diagnosis.get("blocker_detail", "task needs context to continue")
        blocker_type = diagnosis.get("blocker_type", "none")
        resume_prompt_base = diagnosis.get("resume_prompt", f"Continue task {task_id}")
        suggested_fix = diagnosis.get("suggested_fix", "")
        diag_status = diagnosis.get("status", "unknown")

        # Step 2: Search memory for relevant context
        memory_context = ""
        memory_found = False
        try:
            # Build a targeted recall query from the blocker detail
            recall_query = blocker_detail[:200] if blocker_detail else f"context for {task_id}"
            memory_result = await garza_recall(query=recall_query)
            if memory_result and "No memories found" not in memory_result and "not configured" not in memory_result:
                memory_context = memory_result
                memory_found = True
        except Exception:
            memory_context = ""

        # Step 3: Build enriched prompt
        enriched_parts = [
            f"RESUME TASK — Continuing from task {task_id}",
            f"",
            f"Original goal: {resume_prompt_base}",
            f"",
            f"Diagnosis: This task was {diag_status}.",
            f"Blocker: {blocker_detail}",
            f"Suggested fix: {suggested_fix}",
        ]
        if memory_context:
            enriched_parts.append(f"")
            enriched_parts.append(f"Relevant memory context found:")
            enriched_parts.append(memory_context[:1000])
        enriched_parts.append(f"")
        enriched_parts.append(f"Please complete the original goal using the above context. If you encounter the same blocker, use the suggested fix above.")

        enriched_prompt = "\n".join(enriched_parts)

        # Step 4: Create new task with enriched prompt
        new_task_resp = await manus_request("POST", "/tasks", json={
            "prompt": enriched_prompt,
            "taskMode": "agent",
            "agentProfile": "manus-1.6",
        })
        new_task_id = new_task_resp.get("id", new_task_resp.get("task_id", "unknown"))

        # Step 5: Return summary
        lines = [
            f"GARZA OS — Task Resume: {task_id}",
            f"",
            f"=== Diagnosis Summary ===",
            f"Status   : {diag_status}",
            f"Blocker  : {blocker_type} — {blocker_detail[:200]}",
            f"Fix      : {suggested_fix[:200]}",
            f"",
            f"=== Memory Context ===",
            f"Found    : {'Yes — injected into new prompt' if memory_found else 'No — task relies on Manus finding resources itself'}",
        ]
        if memory_found:
            lines.append(f"Preview  : {memory_context[:300]}")
        lines.extend([
            f"",
            f"=== New Task Created ===",
            f"New task ID : {new_task_id}",
            f"Status      : pending",
            f"URL         : https://manus.im/app/{new_task_id}",
            f"",
            f"Tip: Call manus_get_task(task_id='{new_task_id}') in 2-3 minutes to check progress.",
        ])
        return "\n".join(lines)
    except Exception as e:
        return handle_api_error(e)


@mcp.tool()
async def garza_flow_status(hours: int = 24) -> str:
    """Show a high-level workflow view that groups Manus tasks into flows.

    Since the Manus API does not expose parent_id, flows are inferred by:
      - Title similarity: tasks with 'Wide Research Subtask', 'Subtask', 'Sub-task' in the
        name group under the nearest named parent task created within ±10 minutes
      - Time proximity: tasks created within 5 minutes of each other group together

    Output per flow:
      Flow: [parent task title]
        Status: completed | running | stalled | mixed
        Tasks: X total (Y completed, Z running, W failed)
        Duration: Xh Xm total
        Cost: $X.XX
        Bottleneck: [description if any task is stalled/failed]
        Link: [parent task URL]

    Args:
        hours: How many hours back to look (default: 24)
    """
    try:
        import re as _re

        # Fetch all tasks in the time window
        resp = await manus_request("GET", "/tasks", params={"limit": 100})
        all_tasks = resp.get("data", resp) if isinstance(resp, dict) else resp
        if not isinstance(all_tasks, list):
            return "No tasks found."

        now_ts = time.time()
        cutoff = now_ts - (hours * 3600)

        # Filter to time window and parse timestamps
        window_tasks = []
        for t in all_tasks:
            created = t.get("created_at", 0)
            if isinstance(created, str):
                try:
                    created = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
                except Exception:
                    created = 0
            if created >= cutoff:
                t["_created_ts"] = created
                window_tasks.append(t)

        if not window_tasks:
            return f"No tasks found in the last {hours}h."

        # Sort by created_at ascending (oldest first for grouping)
        window_tasks.sort(key=lambda t: t.get("_created_ts", 0))

        # Subtask detection patterns
        _SUBTASK_PATTERNS = [
            r"wide research subtask",
            r"subtask",
            r"sub-task",
            r"sub task",
            r"research subtask",
            r"parallel subtask",
        ]

        def _is_subtask(title: str) -> bool:
            tl = title.lower()
            return any(_re.search(p, tl) for p in _SUBTASK_PATTERNS)

        def _title_similarity(a: str, b: str) -> float:
            """Simple word overlap ratio."""
            wa = set(a.lower().split())
            wb = set(b.lower().split())
            # Remove common subtask words
            noise = {"wide", "research", "subtask", "sub-task", "task", "a", "the", "for", "of", "in", "and"}
            wa -= noise
            wb -= noise
            if not wa or not wb:
                return 0.0
            return len(wa & wb) / max(len(wa), len(wb))

        # Group tasks into flows
        # A "flow" is anchored by a non-subtask task; subtasks attach to the nearest parent
        flows: list[dict] = []  # list of {anchor: task, children: [task]}
        orphans: list[dict] = []

        for task in window_tasks:
            title = task.get("title", task.get("id", ""))
            ts = task.get("_created_ts", 0)

            if _is_subtask(title):
                # Find the best parent: non-subtask task within ±10 min with best title similarity
                best_flow = None
                best_score = -1.0
                for flow in flows:
                    anchor = flow["anchor"]
                    anchor_ts = anchor.get("_created_ts", 0)
                    time_diff = abs(ts - anchor_ts)
                    if time_diff <= 600:  # ±10 minutes
                        sim = _title_similarity(title, anchor.get("title", ""))
                        # Also consider time proximity as a tiebreaker
                        proximity_bonus = max(0, (600 - time_diff) / 600) * 0.3
                        score = sim + proximity_bonus
                        if score > best_score:
                            best_score = score
                            best_flow = flow
                if best_flow is not None:
                    best_flow["children"].append(task)
                else:
                    # No parent found — attach to most recent flow within 10 min
                    for flow in reversed(flows):
                        anchor_ts = flow["anchor"].get("_created_ts", 0)
                        if abs(ts - anchor_ts) <= 600:
                            flow["children"].append(task)
                            break
                    else:
                        orphans.append(task)
            else:
                # Check if this task is close in time to an existing flow (within 5 min)
                merged = False
                for flow in reversed(flows):
                    anchor_ts = flow["anchor"].get("_created_ts", 0)
                    if abs(ts - anchor_ts) <= 300:  # 5 minutes
                        # Only merge if titles are similar
                        sim = _title_similarity(title, flow["anchor"].get("title", ""))
                        if sim > 0.4:
                            flow["children"].append(task)
                            merged = True
                            break
                if not merged:
                    flows.append({"anchor": task, "children": []})

        # Format output
        total_cost = 0
        total_flows = len(flows)
        attention_items = []

        output_lines = [
            f"GARZA OS — Flow Status (last {hours}h)",
            f"{'='*50}",
            "",
        ]

        for flow in sorted(flows, key=lambda f: f["anchor"].get("_created_ts", 0), reverse=True):
            anchor = flow["anchor"]
            all_flow_tasks = [anchor] + flow["children"]

            # Aggregate stats
            statuses_in_flow = [t.get("status", "unknown") for t in all_flow_tasks]
            n_completed = statuses_in_flow.count("completed")
            n_running = sum(1 for s in statuses_in_flow if s in ("running", "pending"))
            n_failed = statuses_in_flow.count("failed")
            n_total = len(all_flow_tasks)

            # Flow status
            if n_failed > 0 and n_running == 0:
                flow_status = "failed"
            elif n_running > 0 and n_failed > 0:
                flow_status = "mixed"
            elif n_running > 0:
                flow_status = "running"
            elif n_completed == n_total:
                flow_status = "completed"
            else:
                flow_status = "mixed"

            # Cost
            flow_credits = 0
            for t in all_flow_tasks:
                cu = t.get("credit_usage", {})
                if isinstance(cu, dict):
                    flow_credits += cu.get("credits", 0)
                elif cu:
                    try:
                        flow_credits += int(cu)
                    except Exception:
                        pass
            total_cost += flow_credits

            # Duration: from earliest created to latest updated
            ts_list = [t.get("_created_ts", 0) for t in all_flow_tasks]
            updated_list = []
            for t in all_flow_tasks:
                upd = t.get("updated_at", 0)
                if isinstance(upd, str):
                    try:
                        upd = datetime.fromisoformat(upd.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        upd = 0
                updated_list.append(upd)
            start_ts = min(ts_list) if ts_list else 0
            end_ts = max(updated_list) if updated_list else 0
            duration_secs = max(0, end_ts - start_ts)
            if duration_secs < 60:
                duration_str = f"{int(duration_secs)}s"
            elif duration_secs < 3600:
                duration_str = f"{int(duration_secs//60)}m {int(duration_secs%60)}s"
            else:
                h = int(duration_secs // 3600)
                m = int((duration_secs % 3600) // 60)
                duration_str = f"{h}h {m}m"

            # Bottleneck
            bottleneck = ""
            if n_failed > 0:
                failed_tasks = [t for t in all_flow_tasks if t.get("status") == "failed"]
                bottleneck = f"{n_failed} task(s) failed — run manus_diagnose_task to investigate"
            elif n_running > 0:
                running_tasks = [t for t in all_flow_tasks if t.get("status") in ("running", "pending")]
                oldest_running = min(running_tasks, key=lambda t: t.get("_created_ts", now_ts))
                run_secs = now_ts - oldest_running.get("_created_ts", now_ts)
                if run_secs > 1800:  # >30 min
                    bottleneck = f"⚠️ Task running {int(run_secs//60)}m — possible runaway"
                    attention_items.append(f"  {anchor.get('title','?')[:50]} — runaway task ({int(run_secs//60)}m)")

            anchor_id = anchor.get("id", "?")
            anchor_title = anchor.get("title", anchor_id)[:60]

            status_icon = {"completed": "✅", "running": "🟡", "failed": "🔴", "mixed": "🟠", "stalled": "⚠️"}.get(flow_status, "❓")

            output_lines.append(f"{status_icon} Flow: {anchor_title}")
            output_lines.append(f"   Status   : {flow_status}")
            output_lines.append(f"   Tasks    : {n_total} total ({n_completed} completed, {n_running} running, {n_failed} failed)")
            output_lines.append(f"   Duration : {duration_str}")
            output_lines.append(f"   Cost     : {_usd(flow_credits)}")
            if bottleneck:
                output_lines.append(f"   Bottleneck: {bottleneck}")
            output_lines.append(f"   Link     : https://manus.im/app/{anchor_id}")
            output_lines.append("")

        # Orphan subtasks (couldn't be grouped)
        if orphans:
            output_lines.append(f"⚪ Ungrouped subtasks: {len(orphans)}")
            for o in orphans[:5]:
                output_lines.append(f"   - {o.get('title', o.get('id','?'))[:60]}")
            if len(orphans) > 5:
                output_lines.append(f"   ... and {len(orphans)-5} more")
            output_lines.append("")

        # Top-level summary
        output_lines.insert(3, f"Summary: {total_flows} flows | Total spend: {_usd(total_cost)} | {len(orphans)} ungrouped subtasks")
        output_lines.insert(4, "")
        if attention_items:
            output_lines.insert(5, "⚠️  Needs Attention:")
            for item in attention_items:
                output_lines.insert(6, item)
            output_lines.insert(7, "")

        return "\n".join(output_lines)
    except Exception as e:
        return handle_api_error(e)


# ===========================================================================
# SPRINT 4 — GARZA OS Agent Fleet Scale Architecture (Parts 1-4, 6)
# ===========================================================================

import re as _re

# ---------------------------------------------------------------------------
# Part 1 — Agent Registry helpers
# ---------------------------------------------------------------------------

_REGISTRY_PATH = "/app/data/agent_registry.json"
_KILL_LOG_PATH = "/app/data/kill_log.json"


def _ensure_data_dir() -> None:
    """Create /app/data directory if it doesn't exist."""
    import os as _os
    _os.makedirs("/app/data", exist_ok=True)


def _registry_load() -> list:
    """Load agent registry from disk. Returns [] if missing."""
    _ensure_data_dir()
    try:
        with open(_REGISTRY_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _registry_save(entries: list) -> None:
    """Persist agent registry to disk."""
    _ensure_data_dir()
    with open(_REGISTRY_PATH, "w") as f:
        json.dump(entries, f, indent=2)


def _slugify(text: str) -> str:
    """Convert text to a URL-safe slug."""
    text = text.lower().strip()
    text = _re.sub(r"[^\w\s-]", "", text)
    text = _re.sub(r"[\s_-]+", "-", text)
    return text[:64].strip("-")


def _auto_agent_name(prompt: str) -> str:
    """Generate an agent name from the first 6 words of a prompt."""
    words = prompt.strip().split()[:6]
    return _slugify(" ".join(words)) or "unnamed-agent"


def _registry_add(task_id: str, prompt: str, agent_name: str = "", agent_purpose: str = "",
                  triggered_by: str = "jaden", tags: list = None,
                  expected_duration_minutes: int = 30) -> dict:
    """Add an entry to the agent registry."""
    entries = _registry_load()
    name = agent_name or _auto_agent_name(prompt)
    entry = {
        "agent_id": name,
        "task_id": task_id,
        "name": agent_name or name,
        "purpose": agent_purpose or prompt[:120],
        "triggered_by": triggered_by,
        "created_at": int(time.time()),
        "tags": tags or [],
        "expected_duration_minutes": expected_duration_minutes,
    }
    # Remove any existing entry for this task_id
    entries = [e for e in entries if e.get("task_id") != task_id]
    entries.append(entry)
    _registry_save(entries)
    return entry


# ---------------------------------------------------------------------------
# Part 1 — Tool: agent_registry_list
# ---------------------------------------------------------------------------

@mcp.tool()
async def agent_registry_list() -> str:
    """
    List all registered agents with their current task status from the Manus API.
    Joins the local agent registry with live task data to show name, purpose, status, and cost.
    """
    try:
        entries = _registry_load()
        if not entries:
            return "Agent registry is empty. Use manus_create_task with agent_name to register agents."

        # Fetch live status for all registered task IDs
        lines = [
            f"GARZA OS — Agent Registry ({len(entries)} agents)",
            "━" * 50,
            "",
        ]
        for entry in sorted(entries, key=lambda e: e.get("created_at", 0), reverse=True):
            task_id = entry.get("task_id", "")
            name = entry.get("name", entry.get("agent_id", "?"))
            purpose = entry.get("purpose", "")[:80]
            triggered_by = entry.get("triggered_by", "?")
            tags = ", ".join(entry.get("tags", [])) or "none"
            created_human = _human_time(entry.get("created_at", 0))

            # Fetch live status
            status = "unknown"
            cost_str = ""
            try:
                task_data = await manus_request("GET", f"/tasks/{task_id}")
                status = task_data.get("status", "unknown")
                credits = task_data.get("credit_usage") or task_data.get("credits_used") or 0
                try:
                    credits_int = int(credits)
                except (TypeError, ValueError):
                    credits_int = 0
                cost_str = _usd(credits_int) if credits_int else ""
            except Exception:
                pass

            status_icon = {"completed": "✅", "running": "🟢", "failed": "🔴",
                           "pending": "⏳", "unknown": "❓"}.get(status, "❓")
            lines.append(f"{status_icon} {name}")
            lines.append(f"   task_id  : {task_id}")
            lines.append(f"   status   : {status}{' · ' + cost_str if cost_str else ''}")
            lines.append(f"   purpose  : {purpose}")
            lines.append(f"   by       : {triggered_by}  |  created: {created_human}")
            lines.append(f"   tags     : {tags}")
            lines.append(f"   url      : https://manus.im/app/{task_id}")
            lines.append("")

        return "\n".join(lines)
    except Exception as e:
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Part 1 — Tool: agent_registry_update
# ---------------------------------------------------------------------------

@mcp.tool()
async def agent_registry_update(
    task_id: str = "",
    agent_id: str = "",
    name: str = "",
    purpose: str = "",
    tags: str = "",
    triggered_by: str = "",
) -> str:
    """
    Update name, purpose, tags, or triggered_by for a registered agent.
    Identify the agent by task_id OR agent_id (slug).
    Args:
        task_id: Manus task ID of the agent to update
        agent_id: Agent slug (alternative to task_id)
        name: New human-readable name
        purpose: New one-sentence purpose description
        tags: Comma-separated tags (replaces existing tags)
        triggered_by: Who triggered this agent (jaden | manus | n8n | auto)
    """
    try:
        if not task_id and not agent_id:
            return "Error: provide task_id or agent_id to identify the agent."

        entries = _registry_load()
        updated = False
        for entry in entries:
            match = (task_id and entry.get("task_id") == task_id) or \
                    (agent_id and entry.get("agent_id") == agent_id)
            if match:
                if name:
                    entry["name"] = name
                if purpose:
                    entry["purpose"] = purpose
                if tags:
                    entry["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
                if triggered_by:
                    entry["triggered_by"] = triggered_by
                entry["updated_at"] = int(time.time())
                updated = True
                break

        if not updated:
            return f"No agent found with task_id={task_id!r} or agent_id={agent_id!r}."

        _registry_save(entries)
        return f"Agent updated successfully.\ntask_id: {task_id or '(by agent_id)'}\nagent_id: {agent_id or '(by task_id)'}"
    except Exception as e:
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Part 2 — garza_fleet_status helpers
# ---------------------------------------------------------------------------

def _classify_task_state(task: dict, now_ts: float) -> str:
    """Classify a task into one of 5 fleet states."""
    status = task.get("status", "unknown")
    updated_raw = task.get("updated_at") or task.get("created_at") or 0
    try:
        updated_ts = float(updated_raw)
    except (TypeError, ValueError):
        updated_ts = 0

    age_secs = now_ts - updated_ts

    if status == "completed":
        return "completed"
    if status in ("failed", "error"):
        return "failed"
    if status in ("running", "pending"):
        if age_secs < 1800:  # < 30 min
            return "active"
        elif age_secs < 7200:  # 30 min – 2h
            return "stalled"
        else:  # > 2h
            return "zombie"
    return "completed" if status == "completed" else "unknown"


def _is_subtask(task: dict) -> bool:
    """Heuristic: detect Wide Research / parallel subtasks."""
    title = (task.get("metadata") or {}).get("task_title", "")
    if not title:
        title = task.get("title", "")
    lower = title.lower()
    return any(kw in lower for kw in [
        "subtask", "sub-task", "wide research", "parallel", "research subtask",
        "research task", "subtask #", "sub task"
    ])


# ---------------------------------------------------------------------------
# Part 2 — Tool: garza_fleet_status
# ---------------------------------------------------------------------------

@mcp.tool()
async def garza_fleet_status(
    filter: str = "all",
) -> str:
    """
    Complete fleet overview across ALL agents — the single command for 100+ concurrent agent ops.
    Fetches all tasks (paginated), classifies into 5 states, collapses subtasks, and shows cost.

    States: active (running <30m), stalled (running 30m-2h), zombie (running >2h), completed, failed.
    Subtasks (Wide Research, parallel) are collapsed under their parent task.

    Args:
        filter: all (default) | attention (zombies + stalled only) | active | completed | today
    """
    try:
        now_ts = time.time()
        registry = _registry_load()
        registry_by_task = {e["task_id"]: e for e in registry}

        # 1. Fetch ALL tasks via pagination
        all_tasks = []
        page = 1
        page_size = 100
        while True:
            try:
                result = await manus_request("GET", "/tasks", params={"limit": page_size, "page": page})
            except Exception:
                break
            batch = result.get("tasks", result.get("data", []))
            if not batch:
                break
            all_tasks.extend(batch)
            has_more = result.get("has_more", False) or result.get("hasMore", False)
            if not has_more or len(batch) < page_size:
                break
            page += 1
            if page > 20:  # safety cap: 2000 tasks max
                break

        if not all_tasks:
            return "No tasks found."

        # 2. Classify every task
        classified = []
        for t in all_tasks:
            state = _classify_task_state(t, now_ts)
            credits = t.get("credit_usage") or t.get("credits_used") or 0
            try:
                credits_int = int(credits)
            except (TypeError, ValueError):
                credits_int = 0
            meta = t.get("metadata") or {}
            title = meta.get("task_title") or t.get("title") or t.get("id", "?")
            created_raw = t.get("created_at") or 0
            updated_raw = t.get("updated_at") or created_raw
            try:
                created_ts = float(created_raw)
                updated_ts = float(updated_raw)
            except (TypeError, ValueError):
                created_ts = updated_ts = 0

            classified.append({
                "id": t.get("id", "?"),
                "title": title,
                "state": state,
                "status": t.get("status", "?"),
                "credits": credits_int,
                "created_ts": created_ts,
                "updated_ts": updated_ts,
                "is_subtask": _is_subtask(t),
                "registry": registry_by_task.get(t.get("id", ""), {}),
            })

        # 3. Apply filter
        today_cutoff = now_ts - 86400
        if filter == "attention":
            show_states = {"zombie", "stalled"}
        elif filter == "active":
            show_states = {"active"}
        elif filter == "completed":
            show_states = {"completed"}
        elif filter == "today":
            classified = [c for c in classified if c["created_ts"] >= today_cutoff]
            show_states = {"active", "stalled", "zombie", "completed", "failed", "unknown"}
        else:
            show_states = {"active", "stalled", "zombie", "completed", "failed", "unknown"}

        visible = [c for c in classified if c["state"] in show_states]

        # 4. Separate named tasks from subtasks
        named = [c for c in visible if not c["is_subtask"]]
        subtasks = [c for c in visible if c["is_subtask"]]

        # 5. Collapse subtasks under nearest named task (±15 min window)
        collapsed_groups: dict = {}  # named_task_id -> list of subtasks
        orphan_subtasks = []
        for sub in subtasks:
            best_match = None
            best_diff = float("inf")
            for named_task in named:
                diff = abs(sub["created_ts"] - named_task["created_ts"])
                if diff < 900 and diff < best_diff:  # 15 min window
                    best_diff = diff
                    best_match = named_task["id"]
            if best_match:
                collapsed_groups.setdefault(best_match, []).append(sub)
            else:
                orphan_subtasks.append(sub)

        # 6. Sort: zombies first, stalled, active, failed, completed
        state_order = {"zombie": 0, "stalled": 1, "active": 2, "failed": 3, "completed": 4, "unknown": 5}
        named.sort(key=lambda c: (state_order.get(c["state"], 5), -c["updated_ts"]))

        # 7. Build output
        total_credits = sum(c["credits"] for c in classified)
        total_tasks = len(classified)
        attention_count = sum(1 for c in classified if c["state"] in ("zombie", "stalled"))

        # Count by state
        state_counts = {}
        for c in classified:
            state_counts[c["state"]] = state_counts.get(c["state"], 0) + 1

        date_str = datetime.fromtimestamp(now_ts).strftime("%B %-d, %Y")
        lines = [
            f"GARZA OS Fleet Status — {date_str}",
            "━" * 50,
            f"💰 {_usd(total_credits)} spent · {total_tasks} tasks · {attention_count} agents need attention",
            "",
        ]

        state_icons = {
            "zombie": "🧟 ZOMBIE",
            "stalled": "🟡 STALLED",
            "active": "🟢 ACTIVE",
            "failed": "🔴 FAILED",
            "completed": "✅ COMPLETED",
        }

        current_state = None
        for task in named:
            state = task["state"]

            # State header
            if state != current_state:
                current_state = state
                count = state_counts.get(state, 0)
                descriptions = {
                    "zombie": "running but silent 2h+",
                    "stalled": "running but no update 30min–2h",
                    "active": "running, updated recently",
                    "failed": "errored out",
                    "completed": "done",
                }
                lines.append(f"{state_icons.get(state, state.upper())} ({count}) — {descriptions.get(state, '')}")

            # Task line
            name = task["registry"].get("name") or task["title"][:55]
            age_secs = now_ts - task["updated_ts"]
            if age_secs < 60:
                age_str = f"{int(age_secs)}s ago"
            elif age_secs < 3600:
                age_str = f"{int(age_secs/60)}m ago"
            elif age_secs < 86400:
                age_str = f"{int(age_secs/3600)}h {int((age_secs%3600)/60)}m ago"
            else:
                age_str = f"{int(age_secs/86400)}d ago"

            duration_secs = task["updated_ts"] - task["created_ts"]
            if duration_secs < 60:
                dur_str = f"{int(duration_secs)}s"
            elif duration_secs < 3600:
                dur_str = f"{int(duration_secs/60)}m {int(duration_secs%60)}s"
            else:
                dur_str = f"{int(duration_secs/3600)}h {int((duration_secs%3600)/60)}m"

            created_dt = datetime.fromtimestamp(task["created_ts"]).strftime("%b %-d") if task["created_ts"] else "?"
            cost_str = _usd(task["credits"]) if task["credits"] else "$0.00"

            kill_hint = "  → KILL" if state == "zombie" else ""
            lines.append(f"  {name:<40}  {created_dt}  {dur_str:<8}  {cost_str:<8}  silent {age_str}{kill_hint}")
            lines.append(f"    https://manus.im/app/{task['id']}")

            # Collapsed subtasks
            subs = collapsed_groups.get(task["id"], [])
            if subs:
                sub_credits = sum(s["credits"] for s in subs)
                sub_states = set(s["state"] for s in subs)
                sub_status = "all completed" if sub_states == {"completed"} else f"{len([s for s in subs if s['state']=='completed'])} completed"
                lines.append(f"    ↳ {len(subs)} subtasks · {sub_status} · {_usd(sub_credits)}")

            lines.append("")

        # Orphan subtasks
        if orphan_subtasks and filter in ("all", "today"):
            lines.append(f"⚪ Ungrouped subtasks: {len(orphan_subtasks)}")
            for s in orphan_subtasks[:3]:
                lines.append(f"   - {s['title'][:60]}")
            if len(orphan_subtasks) > 3:
                lines.append(f"   ... and {len(orphan_subtasks)-3} more")
            lines.append("")

        return "\n".join(lines)
    except Exception as e:
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Part 3 — manus_kill_task
# ---------------------------------------------------------------------------

@mcp.tool()
async def manus_kill_task(
    task_id: str,
    reason: str = "",
) -> str:
    """
    Terminate a running Manus task. Tries DELETE, then POST /cancel, then POST /stop.
    Logs the kill to /app/data/kill_log.json and removes from agent registry.
    Args:
        task_id: The Manus task ID to kill
        reason: Optional reason for killing (logged)
    """
    try:
        # Get current task state before killing
        task_data = {}
        credits_at_kill = 0
        duration_str = ""
        try:
            task_data = await manus_request("GET", f"/tasks/{task_id}")
            credits = task_data.get("credit_usage") or task_data.get("credits_used") or 0
            try:
                credits_at_kill = int(credits)
            except (TypeError, ValueError):
                credits_at_kill = 0
            created_raw = task_data.get("created_at") or 0
            try:
                created_ts = float(created_raw)
                duration_secs = time.time() - created_ts
                if duration_secs < 3600:
                    duration_str = f"{int(duration_secs/60)}m"
                else:
                    duration_str = f"{int(duration_secs/3600)}h {int((duration_secs%3600)/60)}m"
            except (TypeError, ValueError):
                duration_str = "?"
        except Exception:
            pass

        # Try three termination endpoints
        killed = False
        method_used = ""
        for http_method, endpoint in [
            ("DELETE", f"/tasks/{task_id}"),
            ("POST", f"/tasks/{task_id}/cancel"),
            ("POST", f"/tasks/{task_id}/stop"),
        ]:
            try:
                await manus_request(http_method, endpoint)
                killed = True
                method_used = f"{http_method} {endpoint}"
                break
            except Exception:
                continue

        if not killed:
            return (
                f"Failed to kill task {task_id}.\n"
                f"All three termination endpoints failed (DELETE, /cancel, /stop).\n"
                f"The task may have already completed or the API doesn't support termination."
            )

        # Log the kill
        _ensure_data_dir()
        kill_entry = {
            "task_id": task_id,
            "reason": reason or "manual kill",
            "cost_at_kill": _usd(credits_at_kill),
            "credits_at_kill": credits_at_kill,
            "killed_at": int(time.time()),
            "method": method_used,
            "duration": duration_str,
        }
        try:
            kill_log = []
            try:
                with open(_KILL_LOG_PATH) as f:
                    kill_log = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            kill_log.append(kill_entry)
            with open(_KILL_LOG_PATH, "w") as f:
                json.dump(kill_log, f, indent=2)
        except Exception as e:
            logger.warning("[kill_log] Failed to write kill log: %s", e)

        # Remove from registry
        entries = _registry_load()
        entries = [e for e in entries if e.get("task_id") != task_id]
        _registry_save(entries)

        return (
            f"✅ Task killed successfully.\n"
            f"task_id  : {task_id}\n"
            f"method   : {method_used}\n"
            f"cost     : {_usd(credits_at_kill)} ({credits_at_kill} credits)\n"
            f"duration : {duration_str}\n"
            f"reason   : {reason or 'manual kill'}"
        )
    except Exception as e:
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Part 3 — manus_kill_zombies
# ---------------------------------------------------------------------------

@mcp.tool()
async def manus_kill_zombies(
    confirm: bool = False,
) -> str:
    """
    Bulk-kill all zombie tasks (running but silent for 2h+).
    If confirm=False (default), returns a dry-run list of what would be killed.
    If confirm=True, kills all zombies and returns a summary.
    Args:
        confirm: False = dry-run (safe), True = actually kill all zombies
    """
    try:
        now_ts = time.time()

        # Fetch all tasks
        all_tasks = []
        page = 1
        while True:
            try:
                result = await manus_request("GET", "/tasks", params={"limit": 100, "page": page})
            except Exception:
                break
            batch = result.get("tasks", result.get("data", []))
            if not batch:
                break
            all_tasks.extend(batch)
            if not result.get("has_more", False) or len(batch) < 100:
                break
            page += 1
            if page > 20:
                break

        # Find zombies
        zombies = []
        for t in all_tasks:
            if _classify_task_state(t, now_ts) == "zombie":
                credits = t.get("credit_usage") or t.get("credits_used") or 0
                try:
                    credits_int = int(credits)
                except (TypeError, ValueError):
                    credits_int = 0
                meta = t.get("metadata") or {}
                title = meta.get("task_title") or t.get("title") or t.get("id", "?")
                updated_raw = t.get("updated_at") or t.get("created_at") or 0
                try:
                    updated_ts = float(updated_raw)
                    silent_secs = now_ts - updated_ts
                    silent_str = f"{int(silent_secs/3600)}h {int((silent_secs%3600)/60)}m"
                except (TypeError, ValueError):
                    silent_str = "?"
                zombies.append({
                    "id": t.get("id", "?"),
                    "title": title,
                    "credits": credits_int,
                    "silent": silent_str,
                })

        if not zombies:
            return "No zombie tasks found. Fleet is clean."

        total_credits = sum(z["credits"] for z in zombies)

        if not confirm:
            lines = [
                f"🧟 DRY RUN — {len(zombies)} zombie tasks would be killed",
                f"💰 {_usd(total_credits)} would be recovered (credits already spent, but stops future charges)",
                "",
                "Tasks that would be killed:",
            ]
            for z in zombies:
                lines.append(f"  {z['title'][:55]:<55}  silent {z['silent']}  {_usd(z['credits'])}")
                lines.append(f"    https://manus.im/app/{z['id']}")
            lines.append("")
            lines.append("To kill all zombies, call: manus_kill_zombies(confirm=True)")
            return "\n".join(lines)

        # Actually kill them
        killed = []
        failed = []
        for z in zombies:
            result = await manus_kill_task(task_id=z["id"], reason="zombie kill — silent 2h+")
            if "killed successfully" in result:
                killed.append(z)
            else:
                failed.append(z)

        lines = [
            f"🧟 Zombie Kill Complete",
            f"Killed: {len(killed)} tasks | Failed: {len(failed)} tasks",
            f"Credits at kill: {_usd(sum(z['credits'] for z in killed))}",
            "",
        ]
        if killed:
            lines.append("Killed:")
            for z in killed:
                lines.append(f"  ✅ {z['title'][:60]}")
        if failed:
            lines.append("Failed to kill:")
            for z in failed:
                lines.append(f"  ❌ {z['title'][:60]} ({z['id']})")
        return "\n".join(lines)
    except Exception as e:
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Part 4 — manus_triage_task (regex-first blocker pattern library)
# ---------------------------------------------------------------------------

BLOCKER_PATTERNS = {
    "auth_railway":     [r"railway", r"401", r"unauthorized", r"no.*token"],
    "auth_generic":     [r"api.key", r"authentication", r"permission denied", r"403"],
    "missing_tool":     [r"no.*tool", r"tool not found", r"mcp.*unavailable"],
    "waiting_human":    [r"want me to", r"shall i", r"would you like", r"confirm", r"proceed\?"],
    "rate_limit":       [r"rate limit", r"too many requests", r"429", r"quota"],
    "confusion":        [r"i'm not sure", r"unclear", r"could you clarify", r"what do you mean"],
    "timeout":          [r"timed out", r"took too long", r"exceeded.*time"],
    "completed_clean":  [],  # fallback if status=completed
}

_RESUME_HINTS = {
    "auth_railway":   "Add the Railway Bearer token to the MCP server config and retry.",
    "auth_generic":   "Check API key configuration and permissions, then retry.",
    "missing_tool":   "Verify the required MCP tool is installed and the server is running.",
    "waiting_human":  "Respond to Manus's question to continue the task.",
    "rate_limit":     "Wait 60 seconds and retry — or reduce request frequency.",
    "confusion":      "Clarify the task with more specific instructions.",
    "timeout":        "Break the task into smaller steps or increase timeout limits.",
    "completed_clean": "Task completed successfully — no action needed.",
}


@mcp.tool()
async def manus_triage_task(task_id: str) -> str:
    """
    Classify a task's blocker in under 1 second using regex-first pattern matching.
    No LLM call unless regex fails — uses last 500 chars of result text only.
    Returns: blocker_type, confidence, last_words, resume_hint, resume_prompt.
    Args:
        task_id: The Manus task ID to triage
    """
    try:
        t0 = time.time()

        # Fetch task data
        task_data = await manus_request("GET", f"/tasks/{task_id}")
        status = task_data.get("status", "unknown")
        output = task_data.get("output", [])

        # Extract last result text (last 500 chars)
        last_words = ""
        for turn in reversed(output):
            content = turn.get("content", [])
            if isinstance(content, list):
                for part in reversed(content):
                    if isinstance(part, dict) and part.get("type") in ("text", "output_text"):
                        txt = part.get("text", "").strip()
                        if txt:
                            last_words = txt[-500:]
                            break
            elif isinstance(content, str) and content.strip():
                last_words = content.strip()[-500:]
            if last_words:
                break

        # Fast regex pass
        blocker_type = None
        confidence = "low"

        if status == "completed":
            blocker_type = "completed_clean"
            confidence = "high (status=completed)"
        else:
            lower_text = last_words.lower()
            for btype, patterns in BLOCKER_PATTERNS.items():
                if not patterns:
                    continue
                for pattern in patterns:
                    if _re.search(pattern, lower_text):
                        blocker_type = btype
                        confidence = "high (regex match)"
                        break
                if blocker_type:
                    break

        elapsed_ms = int((time.time() - t0) * 1000)

        # Gemini fallback only if regex found nothing and task is still running
        if not blocker_type and status in ("running", "pending", "stalled"):
            try:
                gemini_key = os.environ.get("GEMINI_API_KEY", "")
                if gemini_key and last_words:
                    import requests as _req
                    payload = {
                        "contents": [{"role": "user", "parts": [{"text": (
                            f"Task status: {status}\n"
                            f"Last output: {last_words}\n\n"
                            f"Classify the blocker type from: auth_railway, auth_generic, missing_tool, "
                            f"waiting_human, rate_limit, confusion, timeout, or none.\n"
                            f"Reply with ONLY the blocker type word."
                        )}]}],
                        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 20},
                    }
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
                    resp = _req.post(url, json=payload, timeout=8)
                    resp.raise_for_status()
                    raw = resp.json()
                    llm_answer = raw["candidates"][0]["content"]["parts"][0]["text"].strip().lower()
                    if llm_answer in BLOCKER_PATTERNS:
                        blocker_type = llm_answer
                        confidence = "medium (gemini fallback)"
            except Exception:
                pass

        if not blocker_type:
            blocker_type = "unknown"
            confidence = "low (no pattern match)"

        resume_hint = _RESUME_HINTS.get(blocker_type, "Review the task output and retry with more context.")

        # Build resume prompt
        if blocker_type == "waiting_human":
            resume_prompt = f"Continue the task. Answer: Yes, proceed. Task ID: {task_id}"
        elif blocker_type in ("auth_railway", "auth_generic"):
            resume_prompt = f"Retry the task with correct authentication credentials. Task ID: {task_id}"
        else:
            resume_prompt = f"Resume task {task_id}. Previous attempt stopped due to {blocker_type}. {resume_hint}"

        lines = [
            f"task_id      : {task_id}",
            f"status       : {status}",
            f"blocker_type : {blocker_type}",
            f"confidence   : {confidence}",
            f"elapsed_ms   : {elapsed_ms}ms",
            f"last_words   : {last_words[:120]!r}" if last_words else "last_words   : (no output)",
            f"resume_hint  : {resume_hint}",
            f"resume_prompt: {resume_prompt}",
        ]
        return "\n".join(lines)
    except Exception as e:
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Part 6 — garza_daily_brief
# ---------------------------------------------------------------------------

@mcp.tool()
async def garza_daily_brief() -> str:
    """
    Generate a concise morning briefing in plain prose — designed for n8n/Rube scheduling.
    Calls fleet status, cost summary, and memory recall, then passes to Gemini for a
    5-sentence executive briefing ready to push to Beeper.
    """
    try:
        import requests as _req

        # 1. Fleet status (attention items only)
        fleet_text = await garza_fleet_status(filter="attention")

        # 2. Cost summary (yesterday)
        cost_text = await manus_cost_summary(hours_back=24, group_by="day")

        # 3. Memory recall
        memory_text = ""
        try:
            memory_text = await garza_recall(query="recent decisions and insights", limit=3)
        except Exception:
            memory_text = "(memory unavailable)"

        # 4. Gemini briefing
        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        if not gemini_key:
            return (
                "GARZA OS Daily Brief (no Gemini — raw data):\n\n"
                f"FLEET:\n{fleet_text[:500]}\n\n"
                f"COST:\n{cost_text[:300]}\n\n"
                f"MEMORY:\n{memory_text[:300]}"
            )

        combined = (
            f"FLEET STATUS (attention items):\n{fleet_text[:1500]}\n\n"
            f"YESTERDAY COST:\n{cost_text[:500]}\n\n"
            f"RECENT MEMORY:\n{memory_text[:500]}"
        )

        payload = {
            "contents": [{"role": "user", "parts": [{"text": (
                f"{combined}\n\n"
                "You are GARZA OS. Write a 5-sentence morning briefing for Jaden. "
                "Lead with anything that needs his attention (zombie/stalled tasks). "
                "Include yesterday's total spend and biggest task. "
                "Note any decisions or insights from memory. "
                "Be direct, no bullet points, no markdown."
            )}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 300},
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
        resp = _req.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        raw = resp.json()
        briefing = raw["candidates"][0]["content"]["parts"][0]["text"].strip()

        date_str = datetime.fromtimestamp(time.time()).strftime("%A, %B %-d")
        return f"GARZA OS Daily Brief — {date_str}\n\n{briefing}"

    except Exception as e:
        return handle_api_error(e)
