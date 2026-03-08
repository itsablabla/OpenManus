# Sprint 5: GARZA OS Memory Integration — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fully implement the GARZA OS Memory Integration Plan — fix the broken fabric_call transport, wire memory into every tool's lifecycle (recall on start, remember on insight, consolidate on session end), add 5 new memory-native tools, and deploy v16 to Railway.

**Architecture:** The OpenManus MCP server will be updated to include a stateful `FabricClient` class that manages the Streamable HTTP session with the Fabric.so MCP server. All memory-related tools will use this client. Existing tools will be refactored to call the new memory tools at key lifecycle points.

**Tech Stack:** Python, FastMCP, Starlette, httpx, Dropbox API

---

### Task 1: Fix `fabric_call` Transport

**Files:**
- Modify: `app/mcp/server.py`

**Step 1: Create `FabricClient` class**

Create a new class `FabricClient` to manage the session with the Fabric.so MCP server. It will handle the `initialize` handshake and `Mcp-Session-Id` header automatically.

```python
class FabricClient:
    def __init__(self, base_url, api_key):
        self.base_url = base_url
        self.api_key = api_key
        self.session_id = None
        self.client = httpx.AsyncClient(timeout=20.0)

    async def initialize(self):
        # ... initialize handshake ...
        self.session_id = ...

    async def call(self, tool_name, params):
        if not self.session_id:
            await self.initialize()
        # ... call tool with Mcp-Session-Id header ...

fabric_client = FabricClient(_FABRIC_SO_URL, _FABRIC_SO_API_KEY)
```

**Step 2: Replace `fabric_call` with `fabric_client.call`**

Refactor the existing `fabric_call` function to be a simple wrapper around `fabric_client.call`.

**Step 3: Commit**

```bash
 git commit -m "feat(memory): implement FabricClient for Streamable HTTP transport"
```

---

### Task 2: Wire Memory Into Existing Tools

**Files:**
- Modify: `app/mcp/server.py`

**Step 1: `manus_create_task` → `garza_recall`**

At the start of `manus_create_task`, call `garza_recall` with the task prompt to fetch relevant context and inject it into the prompt.

**Step 2: `manus_triage_task` → `garza_remember`**

When `manus_triage_task` identifies a new blocker pattern, it will call a new `garza_remember` tool to store it as an `Insight` memory.

**Step 3: `garza_fleet_status` → `garza_remember`**

When `garza_fleet_status` detects a zombie or runaway task, it will store it as a `Pattern` memory.

**Step 4: Commit**

```bash
git commit -m "feat(memory): wire recall/remember into create, triage, fleet status"
```

---

### Task 3: Add 5 New Memory-Native Tools

**Files:**
- Modify: `app/mcp/server.py`

**Step 1: Create `garza_remember`**

Create a new tool `garza_remember(content, memory_type, importance, tags)` that wraps `fabric_client.call("agent_remember", ...)`.

**Step 2: Create `garza_learn`**

Create `garza_learn(text)` that calls `fabric_client.call("agent_memory_classify", ...)` and then `agent_remember`.

**Step 3: Create `garza_preferences`**

Create `garza_preferences(preference, importance)` that stores a `Preference` memory.

**Step 4: Create `garza_session_end`**

Create `garza_session_end(summary, decisions, insights)` that wraps `agent_consolidate_session`.

**Step 5: Create `garza_memory_stats`**

Create `garza_memory_stats()` that calls `fabric_client.call("fabric_tag_list", ...)` and `fabric_client.call("fabric_memory_search", ...)` to return a summary of memory contents.

**Step 6: Commit**

```bash
git commit -m "feat(memory): add 5 new memory-native tools"
```

---

### Task 4: Auto-Populate Memory

**Files:**
- Create: `scripts/populate_memory.py`

**Step 1: Create script to scan last 100 tasks**

Write a script that calls `manus_list_tasks(limit=100)`, then for each completed task, calls `manus_diagnose_task` and `manus_triage_task` to extract insights and patterns.

**Step 2: Store insights in memory**

The script will call the new `garza_remember` tool to store the extracted insights.

**Step 3: Commit**

```bash
git commit -m "feat(memory): add script to auto-populate memory from past tasks"
```

---

### Task 5: Deploy

**Step 1: Validate syntax**

```bash
python3 -c "import ast; ast.parse(open('app/mcp/server.py').read())"
```

**Step 2: Bump version and deploy**

```bash
sed -i 's/ARG BUILD_DATE="2026-03-08-v15f"/ARG BUILD_DATE="2026-03-08-v16"/' Dockerfile
git add .
git commit -m "feat(memory): Sprint 5 memory integration complete (v16)"
git push origin main
railway up --service OpenManus
```
