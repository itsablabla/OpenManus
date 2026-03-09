# Task Advancement Engine — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add 4 tools to GARZA OS MCP that allow moving stuck Manus tasks forward by reading their conversation and creating context-aware continuation tasks.

**Architecture:** Conversation-aware continuation pattern — read `output[]` from Manus API, build enriched prompt with full history + new input, create child task via `POST /tasks` with `parent_id`. Store all handoffs in Fabric.so memory.

**Tech Stack:** Python 3.11, FastMCP, httpx, existing `manus_request()` + `fabric_call()` helpers, `BLOCKER_PATTERNS` dict from `manus_triage_task`.

**File to modify:** `app/mcp/server.py` — append 4 new `@mcp.tool()` functions after the existing `garza_memory_inject` tool (end of file).

---

## Task 1: `manus_task_read_conversation`

**Files:**
- Modify: `app/mcp/server.py` (append at end)
- Test: manual acceptance test via MCP client

**Step 1: Write the failing test**

```python
# Test: call manus_task_read_conversation on a real task
# Expected: returns conversation with "=== CONVERSATION ===" header and last message
result = await call_tool(session, "manus_task_read_conversation", {
    "task_id": REAL_TASK_ID
})
assert "=== CONVERSATION ===" in result
assert "Last message" in result
```

**Step 2: Implement the tool**

Append to `app/mcp/server.py`:

```python
# ---------------------------------------------------------------------------
# Sprint 7 — Task Advancement Engine
# ---------------------------------------------------------------------------

@mcp.tool()
async def manus_task_read_conversation(task_id: str, last_n: int = 0) -> str:
    """
    Read the full conversation from a Manus task — what it's saying, what it's asking for,
    and whether it's blocked.

    Use this before manus_task_unblock or manus_task_advance to understand what a task needs.

    Args:
        task_id: The Manus task ID to read
        last_n: If >0, only show the last N messages (default 0 = show all)
    """
    try:
        detail = await manus_request("GET", f"/tasks/{task_id}")
    except Exception as e:
        return handle_api_error(e)

    status = detail.get("status", "unknown")
    output = detail.get("output", [])
    meta = detail.get("metadata") or {}
    title = meta.get("task_title", task_id)
    credits = detail.get("credit_usage", 0)

    # Parse messages from output[]
    messages = []
    for item in output:
        role = item.get("role", "unknown")
        content_list = item.get("content") or []
        for c in content_list:
            if c.get("type") == "output_text":
                text = c.get("text", "").strip()
                if text:
                    messages.append({"role": role, "text": text})

    if last_n > 0:
        messages = messages[-last_n:]

    # Detect blocker in last assistant message
    last_assistant = ""
    for m in reversed(messages):
        if m["role"] == "assistant":
            last_assistant = m["text"]
            break

    blocker_type = "none"
    blocker_detail = ""
    if last_assistant:
        import re
        for btype, patterns in BLOCKER_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, last_assistant, re.IGNORECASE):
                    blocker_type = btype
                    blocker_detail = last_assistant[:300]
                    break
            if blocker_type != "none":
                break

    lines = [
        f"Task: {title}",
        f"ID: {task_id}",
        f"Status: {status} | Credits: {_usd(credits if isinstance(credits, (int,float)) else 0)}",
        f"Messages: {len(messages)}",
        "",
        "=== CONVERSATION ===",
    ]

    for i, m in enumerate(messages, 1):
        role_label = "👤 USER" if m["role"] == "user" else "🤖 MANUS"
        lines.append(f"\n[{i}] {role_label}")
        lines.append(m["text"][:500] + ("..." if len(m["text"]) > 500 else ""))

    lines.extend([
        "",
        "=== LAST MESSAGE ===",
        last_assistant[:600] if last_assistant else "(no assistant messages yet)",
        "",
        f"=== BLOCKER DETECTION ===",
        f"Type: {blocker_type}",
    ])
    if blocker_detail:
        lines.append(f"Detail: {blocker_detail[:200]}")

    if blocker_type == "waiting_human":
        lines.append("")
        lines.append("→ Call manus_task_unblock(task_id, answer='your reply') to continue this task.")
    elif blocker_type != "none":
        lines.append("")
        lines.append("→ Call manus_task_advance(task_id) to auto-unblock using memory.")

    return "\n".join(lines)
```

**Step 3: Validate syntax**

```bash
cd /tmp/OpenManus && python3 -m py_compile app/mcp/server.py && echo "OK"
```

Expected: `OK`

**Step 4: Commit**

```bash
git add app/mcp/server.py
git commit -m "feat: add manus_task_read_conversation"
```

---

## Task 2: `manus_task_unblock`

**Files:**
- Modify: `app/mcp/server.py` (append after Task 1)

**Step 1: Write the failing test**

```python
# Test: unblock a completed task (safe — creates child but original is done)
result = await call_tool(session, "manus_task_unblock", {
    "task_id": COMPLETED_TASK_ID,
    "answer": "test answer — ignore this task",
    "include_full_history": False
})
assert "New task" in result or "continuation" in result.lower()
assert "manus.im/app/" in result
```

**Step 2: Implement the tool**

```python
@mcp.tool()
async def manus_task_unblock(
    task_id: str,
    answer: str,
    additional_context: str = "",
    include_full_history: bool = True,
) -> str:
    """
    Reply to a blocked task by creating a context-aware continuation task.

    Since the Manus API does not support mid-task replies, this creates a new task
    with the full conversation history + your answer baked into the prompt.
    The new task picks up exactly where the old one left off.

    Args:
        task_id: The blocked task to continue
        answer: Your reply — the information, credentials, or decision the task needs
        additional_context: Optional extra context to inject (e.g., related docs, links)
        include_full_history: If True (default), include full conversation in new prompt.
                              Set False for very long tasks to keep prompt concise.
    """
    try:
        detail = await manus_request("GET", f"/tasks/{task_id}")
    except Exception as e:
        return handle_api_error(e)

    status = detail.get("status", "unknown")
    output = detail.get("output", [])
    meta = detail.get("metadata") or {}
    title = meta.get("task_title", task_id)

    # Extract original user prompt (first user message)
    original_prompt = ""
    for item in output:
        if item.get("role") == "user":
            for c in (item.get("content") or []):
                if c.get("type") == "output_text":
                    original_prompt = c.get("text", "")
                    break
        if original_prompt:
            break

    # Build conversation transcript
    transcript_lines = []
    if include_full_history:
        for item in output:
            role = item.get("role", "?")
            role_label = "USER" if role == "user" else "MANUS"
            for c in (item.get("content") or []):
                if c.get("type") == "output_text":
                    text = c.get("text", "").strip()
                    if text:
                        transcript_lines.append(f"[{role_label}]: {text[:800]}")

    # Build continuation prompt
    parts = [
        f"CONTINUATION — Picking up from task {task_id}: {title}",
        "",
        "=== ORIGINAL GOAL ===",
        original_prompt or f"Complete the task: {title}",
    ]

    if transcript_lines:
        parts.extend([
            "",
            "=== CONVERSATION HISTORY ===",
            "\n".join(transcript_lines[-20:]),  # last 20 exchanges max
        ])

    parts.extend([
        "",
        "=== USER REPLY ===",
        answer,
    ])

    if additional_context:
        parts.extend([
            "",
            "=== ADDITIONAL CONTEXT ===",
            additional_context,
        ])

    parts.extend([
        "",
        "=== INSTRUCTIONS ===",
        "Continue from where the previous task left off.",
        "The user has provided the information above.",
        "Use it to complete the original goal.",
        "Do not ask for information that was already provided above.",
    ])

    continuation_prompt = "\n".join(parts)

    # Create continuation task
    try:
        resp = await manus_request("POST", "/tasks", json={
            "prompt": continuation_prompt,
            "taskMode": "agent",
            "agentProfile": "manus-1.6",
            "parent_id": task_id,
        })
    except Exception as e:
        return handle_api_error(e)

    new_task_id = resp.get("task_id", resp.get("id", "unknown"))
    new_task_url = resp.get("task_url", f"https://manus.im/app/{new_task_id}")

    # Store handoff in memory
    asyncio.ensure_future(fabric_call("agent_remember", {
        "content": f"Task handoff: {task_id} → {new_task_id}. Title: {title}. Injected: {answer[:100]}",
        "importance": 0.8,
        "tags": ["task-handoff", "task-advancement", "garza-os"],
    }))

    return "\n".join([
        f"✅ Continuation task created",
        f"",
        f"Original task : {task_id} ({status})",
        f"Title         : {title}",
        f"Your answer   : {answer[:120]}{'...' if len(answer) > 120 else ''}",
        f"",
        f"New task ID   : {new_task_id}",
        f"URL           : {new_task_url}",
        f"",
        f"History included : {'Yes (' + str(len(transcript_lines)) + ' exchanges)' if transcript_lines else 'No'}",
        f"",
        f"Tip: Call manus_get_task(task_id='{new_task_id}') in 2-3 min to check progress.",
    ])
```

**Step 3: Validate syntax**

```bash
python3 -m py_compile app/mcp/server.py && echo "OK"
```

**Step 4: Commit**

```bash
git commit -am "feat: add manus_task_unblock"
```

---

## Task 3: `manus_task_advance`

**Files:**
- Modify: `app/mcp/server.py` (append after Task 2)

**Step 1: Write the failing test**

```python
result = await call_tool(session, "manus_task_advance", {
    "task_id": REAL_TASK_ID,
    "dry_run": True
})
assert "Detected" in result or "blocker" in result.lower()
assert "Confidence" in result
```

**Step 2: Implement the tool**

```python
@mcp.tool()
async def manus_task_advance(
    task_id: str,
    hint: str = "",
    dry_run: bool = False,
) -> str:
    """
    Autonomously unblock a stuck task using memory, AI reasoning, and pattern matching.
    No human input required — it figures out what the task needs and injects it.

    Steps:
    1. Reads the full task conversation
    2. Detects what the task is blocked on (credentials, clarification, tool, etc.)
    3. Searches memory for relevant context using garza_learn
    4. Constructs the best possible continuation prompt
    5. Creates a continuation task (or dry-run preview)

    Args:
        task_id: The stuck task to advance
        hint: Optional hint to guide memory search (e.g., 'check Namecheap credentials')
        dry_run: If True, shows what it would do without creating a task
    """
    try:
        detail = await manus_request("GET", f"/tasks/{task_id}")
    except Exception as e:
        return handle_api_error(e)

    status = detail.get("status", "unknown")
    output = detail.get("output", [])
    meta = detail.get("metadata") or {}
    title = meta.get("task_title", task_id)

    # Extract last assistant message
    last_assistant = ""
    original_prompt = ""
    transcript_lines = []

    for item in output:
        role = item.get("role", "?")
        for c in (item.get("content") or []):
            if c.get("type") == "output_text":
                text = c.get("text", "").strip()
                if text:
                    role_label = "USER" if role == "user" else "MANUS"
                    transcript_lines.append(f"[{role_label}]: {text[:600]}")
                    if role == "user" and not original_prompt:
                        original_prompt = text
                    if role == "assistant":
                        last_assistant = text

    # Detect blocker
    import re
    blocker_type = "none"
    blocker_detail = last_assistant[:400] if last_assistant else ""

    for btype, patterns in BLOCKER_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, last_assistant, re.IGNORECASE):
                blocker_type = btype
                break
        if blocker_type != "none":
            break

    # Build memory search query
    if hint:
        search_query = hint
    elif blocker_detail:
        search_query = blocker_detail[:200]
    else:
        search_query = f"{title} context credentials"

    # Search memory
    memory_result = await garza_learn(query=search_query, limit=5)
    memory_found = memory_result and "No memories found" not in memory_result

    # Determine confidence
    if memory_found and blocker_type != "none":
        confidence = 0.85
        answer_source = "memory (exact blocker match)"
    elif memory_found:
        confidence = 0.65
        answer_source = "memory (partial match)"
    else:
        confidence = 0.30
        answer_source = "no memory found — generic continuation"
        memory_result = f"No relevant memory found for: {search_query}"

    # Build continuation prompt
    parts = [
        f"CONTINUATION — Auto-advancing task {task_id}: {title}",
        "",
        "=== ORIGINAL GOAL ===",
        original_prompt or f"Complete the task: {title}",
    ]

    if transcript_lines:
        parts.extend([
            "",
            "=== CONVERSATION HISTORY ===",
            "\n".join(transcript_lines[-15:]),
        ])

    parts.extend([
        "",
        "=== CONTEXT FROM GARZA OS MEMORY ===",
        memory_result[:1500] if memory_result else "(none found)",
        "",
        "=== INSTRUCTIONS ===",
        "Continue from where the previous task left off.",
        "Use the memory context above to resolve any blockers.",
        "If the context above contains credentials or answers, use them directly.",
        "Complete the original goal.",
    ])

    continuation_prompt = "\n".join(parts)

    # Build response
    lines = [
        f"GARZA OS — Auto-Advance: {task_id}",
        f"",
        f"Task     : {title}",
        f"Status   : {status}",
        f"Blocker  : {blocker_type}",
        f"Detail   : {blocker_detail[:200] if blocker_detail else 'none detected'}",
        f"",
        f"Memory search: '{search_query[:100]}'",
        f"Memory found : {'Yes' if memory_found else 'No'}",
        f"Answer source: {answer_source}",
        f"Confidence   : {confidence:.0%}",
    ]

    if dry_run:
        lines.extend([
            "",
            "=== DRY RUN — No task created ===",
            "Continuation prompt preview:",
            continuation_prompt[:800] + "...",
            "",
            "Run with dry_run=False to create the continuation task.",
        ])
        return "\n".join(lines)

    # Create continuation task
    try:
        resp = await manus_request("POST", "/tasks", json={
            "prompt": continuation_prompt,
            "taskMode": "agent",
            "agentProfile": "manus-1.6",
            "parent_id": task_id,
        })
    except Exception as e:
        return handle_api_error(e)

    new_task_id = resp.get("task_id", resp.get("id", "unknown"))
    new_task_url = resp.get("task_url", f"https://manus.im/app/{new_task_id}")

    # Store handoff in memory
    asyncio.ensure_future(fabric_call("agent_remember", {
        "content": (
            f"Auto-advance: {task_id} → {new_task_id}. "
            f"Blocker: {blocker_type}. "
            f"Source: {answer_source}. "
            f"Confidence: {confidence:.0%}."
        ),
        "importance": 0.75,
        "tags": ["task-handoff", "task-advance", "auto-advance", "garza-os"],
    }))

    lines.extend([
        "",
        f"✅ Continuation task created",
        f"New task ID : {new_task_id}",
        f"URL         : {new_task_url}",
        f"",
        f"Tip: Call manus_get_task(task_id='{new_task_id}') in 2-3 min to check progress.",
    ])

    return "\n".join(lines)
```

**Step 3: Validate syntax**

```bash
python3 -m py_compile app/mcp/server.py && echo "OK"
```

**Step 4: Commit**

```bash
git commit -am "feat: add manus_task_advance"
```

---

## Task 4: `manus_task_handoff_log`

**Files:**
- Modify: `app/mcp/server.py` (append after Task 3)

**Step 1: Write the failing test**

```python
result = await call_tool(session, "manus_task_handoff_log", {})
assert "Handoff" in result or "No handoffs" in result
```

**Step 2: Implement the tool**

```python
@mcp.tool()
async def manus_task_handoff_log(task_id: str = "", limit: int = 20) -> str:
    """
    View the history of all task handoffs — parent → child task chains created by
    manus_task_unblock and manus_task_advance.

    Args:
        task_id: If provided, filter to handoffs involving this task ID.
                 If empty, show all recent handoffs.
        limit: Max handoffs to show (default 20)
    """
    query = f"task handoff {task_id}" if task_id else "task handoff task-advancement garza-os"
    result = await fabric_call("fabric_memory_search", {"query": query, "limit": limit})
    hits = result.get("hits", [])

    # Filter to only handoff memories
    handoffs = [h for h in hits if any(
        tag in str(h.get("name", "") + h.get("content", ""))
        for tag in ["handoff", "task-advance", "continuation"]
    )]

    if not handoffs:
        return (
            "No task handoffs recorded yet.\n"
            "Handoffs are created when you call manus_task_unblock() or manus_task_advance()."
        )

    lines = [
        f"Task Handoff Log — {len(handoffs)} records",
        "=" * 50,
        "",
    ]

    for i, h in enumerate(handoffs, 1):
        content = h.get("content", h.get("name", ""))
        created = h.get("createdAt", "")
        age = _human_time(created) if created else ""
        lines.append(f"{i}. {content[:200]}")
        if age:
            lines.append(f"   ({age})")
        lines.append("")

    if task_id:
        lines.append(f"Filtered to task: {task_id}")

    return "\n".join(lines)
```

**Step 3: Validate syntax**

```bash
python3 -m py_compile app/mcp/server.py && echo "OK"
```

**Step 4: Commit**

```bash
git commit -am "feat: add manus_task_handoff_log"
```

---

## Task 5: Bump Version + Deploy

**Step 1: Bump server name**

```python
# In app/mcp/server.py, change:
mcp = FastMCP("OpenManus Hybrid MCP Server v19 — Read-Only Task Management")
# To:
mcp = FastMCP("OpenManus Hybrid MCP Server v20 — Task Advancement Engine")
```

**Step 2: Final syntax check + tool count**

```bash
python3 -m py_compile app/mcp/server.py && echo "OK"
grep -c "^@mcp.tool()" app/mcp/server.py
# Expected: 34
```

**Step 3: Commit + push + deploy**

```bash
git add -A
git commit -m "feat: v20 — Task Advancement Engine (4 new tools)"
git push origin main
railway deployment up
```

**Step 4: Wait for deploy (~6 min) then run acceptance tests**

---

## Acceptance Tests

```python
# All 4 tools must pass:
assert tool_count == 34
assert "manus_task_read_conversation" in tool_names
assert "manus_task_unblock" in tool_names
assert "manus_task_advance" in tool_names
assert "manus_task_handoff_log" in tool_names

# manus_task_read_conversation
result = await call_tool("manus_task_read_conversation", {"task_id": REAL_TASK_ID})
assert "=== CONVERSATION ===" in result
assert "=== LAST MESSAGE ===" in result

# manus_task_unblock (use completed task to avoid polluting running ones)
result = await call_tool("manus_task_unblock", {
    "task_id": COMPLETED_TASK_ID,
    "answer": "acceptance test — ignore this task",
    "include_full_history": False
})
assert "manus.im/app/" in result

# manus_task_advance dry_run
result = await call_tool("manus_task_advance", {
    "task_id": REAL_TASK_ID,
    "dry_run": True
})
assert "DRY RUN" in result
assert "Confidence" in result

# manus_task_handoff_log
result = await call_tool("manus_task_handoff_log", {})
assert "Handoff" in result or "No handoffs" in result
```

---

*Implementation plan complete. Proceed to superpowers:executing-plans.*
