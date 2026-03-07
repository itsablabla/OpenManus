"""
Hybrid MCP Server for GARZA OS — OpenManus + Manus API.

Synchronous layer  : bash, browser, editor, terminate (OpenManus local agents)
Asynchronous layer : manus_create_task, manus_get_task, manus_list_tasks,
                     manus_upload_file, manus_list_files, manus_create_webhook
                     (Manus SaaS API — fire-and-forget, poll for results)
"""

import asyncio
import logging
import os
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
# OAuth2 well-known endpoints (required for Nango mcp-generic / MCP_OAUTH2_GENERIC)
# Enables Nango to discover and connect to this server via the Connect flow.
# Auth is implemented as a pass-through: the MCP_SERVER_AUTH_TOKEN is used
# as both the authorization code and the access token.
# ---------------------------------------------------------------------------

_BASE_URL = "https://" + os.environ.get(
    "RAILWAY_PUBLIC_DOMAIN", "openmanus-mcp-production.up.railway.app"
)


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def oauth_authorization_server(request: Request) -> JSONResponse:
    """RFC 8414 — OAuth 2.0 Authorization Server Metadata (required by Nango mcp-generic)."""
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
    """RFC 9728 — OAuth 2.0 Protected Resource Metadata (required by Nango mcp-generic)."""
    return JSONResponse({
        "resource": _BASE_URL,
        "authorization_servers": [_BASE_URL],
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://github.com/itsablabla/OpenManus",
    })


@mcp.custom_route("/oauth/authorize", methods=["GET"])
async def oauth_authorize(request: Request) -> RedirectResponse:
    """OAuth2 authorization endpoint — redirects with MCP_SERVER_AUTH_TOKEN as the code."""
    redirect_uri = request.query_params.get("redirect_uri", "")
    state = request.query_params.get("state", "")
    code = os.environ.get("MCP_SERVER_AUTH_TOKEN", "")
    return RedirectResponse(url=f"{redirect_uri}?code={code}&state={state}")


@mcp.custom_route("/oauth/token", methods=["POST"])
async def oauth_token(request: Request) -> JSONResponse:
    """OAuth2 token endpoint — exchanges authorization code for access token."""
    form = await request.form()
    code = str(form.get("code", ""))
    token = code or os.environ.get("MCP_SERVER_AUTH_TOKEN", "")
    return JSONResponse({
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": 31536000,  # 1 year
        "scope": "mcp",
    })


# ---------------------------------------------------------------------------
# Synchronous tools (existing OpenManus agents — unchanged from upstream)
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
        # Map friendly aliases to official API values
        profile_map = {"speed": "manus-1.6", "quality": "manus-1.6-max", "lite": "manus-1.6-lite"}
        api_profile = profile_map.get(agent_profile, agent_profile)
        body: dict = {
            "prompt": prompt,
            "taskMode": task_mode,
            "agentProfile": api_profile,
        }

        if file_ids:
            body["attachments"] = [{"type": "file_id", "file_id": fid.strip()} for fid in file_ids.split(",") if fid.strip()]

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
        task_id = result.get("id", result.get("task_id", "unknown"))
        status = result.get("status", "pending")

        return (
            f"Task created successfully.\n"
            f"task_id : {task_id}\n"
            f"status  : {status}\n"
            f"Tip     : Call manus_get_task(task_id='{task_id}') in 2-3 minutes to check progress."
        )
    except Exception as e:
        return handle_api_error(e)


@mcp.tool()
async def manus_get_task(task_id: str) -> str:
    """
    Poll the status and output of a Manus task.

    Args:
        task_id: The task ID returned by manus_create_task
    """
    try:
        result = await manus_request("GET", f"/tasks/{task_id}")
        status = result.get("status", "unknown")
        output = result.get("output") or result.get("result") or ""
        artifacts = result.get("artifacts", [])
        credits_used = result.get("credits_used")
        error_msg = result.get("error_message") or result.get("error") or ""

        lines = [
            f"task_id     : {task_id}",
            f"status      : {status}",
        ]
        if credits_used is not None:
            lines.append(f"credits_used: {credits_used}")
        if output:
            lines.append(f"\nOutput:\n{output}")
        if artifacts:
            lines.append(f"\nArtifacts ({len(artifacts)}):")
            for a in artifacts[:10]:
                lines.append(f"  - {a.get('name', 'file')} → {a.get('url', '(no url)')}")
        if error_msg:
            lines.append(f"\nError: {error_msg}")
        if status in ("pending", "running"):
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
    List recent Manus tasks.

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

        lines = [f"Tasks ({len(tasks)} returned):"]
        for t in tasks:
            tid = t.get("id", t.get("task_id", "?"))
            tstatus = t.get("status", "?")
            created = t.get("created_at", "")[:19]
            meta = t.get("metadata") or {}
            title = meta.get("task_title") or (t.get("prompt", "") or "")[:60]
            url = meta.get("task_url", "")
            lines.append(f"  {tid}  [{tstatus}]  {created}  {title!r}  {url}")

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

        # Step 1: Get presigned upload URL
        presign = await manus_request("POST", "/files", json={
            "filename": filename,
            "content_type": content_type,
        })
        upload_url = presign.get("upload_url")
        file_id = presign.get("id") or presign.get("file_id")

        if not upload_url or not file_id:
            return f"Error: Unexpected presign response — {presign}"

        # Step 2: Upload via PUT
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
            created = (f.get("created_at") or "")[:19]
            lines.append(f"  {fid}  {name}  {size} bytes  {created}")

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

    Use your n8n webhook trigger URL so GARZA OS is notified automatically
    when long-running tasks finish — no polling required.

    Args:
        url: Webhook endpoint URL (e.g., your n8n webhook trigger URL)
        events: Comma-separated events (default: task.completed,task.failed)
    """
    try:
        event_list = [e.strip() for e in events.split(",") if e.strip()]
        result = await manus_request("POST", "/webhooks", json={
            "url": url,
            "events": event_list,
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
