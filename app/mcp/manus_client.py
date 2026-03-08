"""
Manus API client for the hybrid MCP server.

Centralizes all interaction with api.manus.ai — authentication,
request execution, and actionable error formatting.

Authentication: API_KEY header (not Bearer token)
Base URL: https://api.manus.ai/v1
"""

import os
from typing import Any, Dict, Optional

import httpx

MANUS_BASE_URL = "https://api.manus.ai/v1"


def _get_headers() -> Dict[str, str]:
    api_key = os.environ.get("MANUS_API_KEY", "")
    if not api_key:
        raise ValueError(
            "MANUS_API_KEY environment variable is not set. "
            "Add it in Railway -> Variables."
        )
    return {
        "API_KEY": api_key,
        "Content-Type": "application/json",
    }


def handle_api_error(e: Exception) -> str:
    """Return a clear, actionable error string for any Manus API exception."""
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status == 401:
            return (
                "Error: Invalid MANUS_API_KEY. "
                "Check your Railway environment variables."
            )
        if status == 402:
            return "Error: Insufficient Manus credits. Top up at manus.im/billing."
        if status == 404:
            return "Error: Resource not found. Check the task_id or file_id is correct."
        if status == 429:
            return "Error: Manus API rate limit hit. Wait 60 seconds and retry."
        return f"Error: Manus API returned {status}: {e.response.text}"
    if isinstance(e, httpx.TimeoutException):
        return (
            "Error: Request timed out after 30s. "
            "Manus tasks are async — use manus_get_task to poll for results."
        )
    if isinstance(e, ValueError):
        return f"Error: Configuration issue — {str(e)}"
    return f"Error: {type(e).__name__}: {str(e)}"


async def manus_request(
    method: str,
    path: str,
    json: Optional[Dict] = None,
    params: Optional[Dict] = None,
) -> Any:
    """Execute an authenticated request against the Manus API."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(
            method=method,
            url=f"{MANUS_BASE_URL}{path}",
            headers=_get_headers(),
            json=json,
            params={k: v for k, v in (params or {}).items() if v is not None},
        )
        response.raise_for_status()
        return response.json()
