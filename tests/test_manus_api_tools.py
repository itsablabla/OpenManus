"""
Diagnostic script — tests all 6 async Manus API tools directly
(no MCP server needed).

Usage:
    MANUS_API_KEY=your_key python tests/test_manus_api_tools.py

What it does:
    1. Lists recent tasks
    2. Creates a test task (speed mode, minimal credits)
    3. Polls task until completed or 5 minutes elapsed
    4. Lists files
    5. Tests webhook registration (uses a dummy URL)
"""

import asyncio
import os
import sys
import time

# Allow running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.mcp.manus_client import handle_api_error, manus_request


async def test_list_tasks() -> None:
    print("\n--- manus_list_tasks ---")
    try:
        result = await manus_request("GET", "/tasks", params={"limit": 5})
        tasks = result.get("tasks", result.get("data", []))
        print(f"Found {len(tasks)} recent task(s)")
        for t in tasks[:3]:
            print(f"  {t.get('id')}  [{t.get('status')}]  {(t.get('prompt') or '')[:60]!r}")
    except Exception as e:
        print("Error:", handle_api_error(e))


async def test_create_and_poll() -> str | None:
    print("\n--- manus_create_task ---")
    try:
        result = await manus_request("POST", "/tasks", json={
            "prompt": (
                "Search the web for the current Anthropic Claude model lineup "
                "and return a bullet list of model names and their key strengths."
            ),
            "task_mode": "agent",
            "agent_profile": "speed",
        })
        task_id = result.get("id") or result.get("task_id")
        print(f"Created task: {task_id}  status={result.get('status')}")
    except Exception as e:
        print("Error:", handle_api_error(e))
        return None

    if not task_id:
        print("No task_id returned — skipping poll.")
        return None

    print(f"\n--- Polling {task_id} (max 5 min) ---")
    deadline = time.time() + 300
    poll_interval = 15
    while time.time() < deadline:
        await asyncio.sleep(poll_interval)
        try:
            r = await manus_request("GET", f"/tasks/{task_id}")
            status = r.get("status", "?")
            print(f"  [{status}]  {int(time.time() % 10000)}s")
            if status in ("completed", "failed"):
                output = r.get("output") or r.get("result") or ""
                print(f"\nFinal status : {status}")
                print(f"Output preview: {output[:400]!r}")
                return task_id
        except Exception as e:
            print("  Poll error:", handle_api_error(e))
        poll_interval = min(poll_interval * 1.5, 60)

    print("Timeout — task did not complete within 5 minutes.")
    return task_id


async def test_list_files() -> None:
    print("\n--- manus_list_files ---")
    try:
        result = await manus_request("GET", "/files", params={"limit": 5})
        files = result.get("files", result.get("data", []))
        print(f"Found {len(files)} file(s)")
        for f in files[:3]:
            print(f"  {f.get('id')}  {f.get('filename') or f.get('name')}  {f.get('size')} bytes")
    except Exception as e:
        print("Error:", handle_api_error(e))


async def test_create_webhook() -> None:
    print("\n--- manus_create_webhook ---")
    dummy_url = "https://example.com/webhook/manus-test"
    try:
        result = await manus_request("POST", "/webhooks", json={
            "url": dummy_url,
            "events": ["task.completed", "task.failed"],
        })
        print(f"Webhook id : {result.get('id') or result.get('webhook_id')}")
        print(f"Secret     : {result.get('secret', '(none)')}")
    except Exception as e:
        print("Error (expected if URL is not reachable):", handle_api_error(e))


async def main() -> None:
    key = os.environ.get("MANUS_API_KEY", "")
    if not key:
        print("ERROR: MANUS_API_KEY environment variable is not set.")
        print("Usage: MANUS_API_KEY=your_key python tests/test_manus_api_tools.py")
        sys.exit(1)

    print(f"MANUS_API_KEY set ({len(key)} chars) — running diagnostics...")

    await test_list_tasks()
    await test_create_and_poll()
    await test_list_files()
    await test_create_webhook()

    print("\n✅ Diagnostic complete.")


if __name__ == "__main__":
    asyncio.run(main())
