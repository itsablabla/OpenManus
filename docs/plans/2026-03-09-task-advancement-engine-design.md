# Task Advancement Engine — Design Document

**Date:** 2026-03-09  
**Sprint:** 7  
**Target:** GARZA OS MCP v20  
**Author:** GARZA OS via superpowers:brainstorming

---

## Problem Statement

The largest weakness in GARZA OS MCP is its inability to move existing tasks forward.
The system can watch, triage, diagnose, and resume tasks — but "resume" creates a brand-new
task from scratch rather than continuing the blocked one. There is no way to:

1. Read what a task is currently saying / asking for
2. Reply to a task that is waiting for input
3. Inject context, credentials, or answers into a stuck task
4. Autonomously unblock a task using memory + AI reasoning

### API Constraint (Confirmed via Probe)

The Manus API has **no native endpoint to reply to or update a running task**:
- `POST /tasks/{id}/messages` → 404
- `POST /tasks/{id}/reply` → 404
- `POST /tasks/{id}/input` → 404
- `PATCH /tasks/{id}` → 405

**What IS available:**
- `GET /tasks/{id}` returns full `output[]` conversation array (all messages, in order)
- `POST /tasks` with `parent_id` creates a continuation task that inherits context
- Task `output[]` contains the full conversation: user messages, assistant messages, tool calls

---

## Approach: Conversation-Aware Continuation

Since we cannot inject into a live task, we use a **conversation-aware continuation** pattern:

1. Read the full conversation from the blocked task
2. Build a new prompt that includes: original goal + full conversation history + new input
3. Create a new task via `POST /tasks` with `parent_id` linking to the original
4. The new task has full context and picks up exactly where the old one left off

This is the correct pattern — it's how Manus itself handles follow-up tasks in the UI.

---

## Design: 4 New Tools

### Tool 1: `manus_task_read_conversation`

**Purpose:** Read and display the full conversation from any task.

**Input:**
- `task_id: str` — the task to read
- `last_n: int = 0` — if >0, only show last N messages (default: all)

**Output:** Formatted conversation showing:
- Task title and status
- Each message: role (user/assistant), type, text content
- Last message highlighted (what the task is currently saying)
- Blocker detection: is it asking a question? Waiting for credentials? Stuck?

**Implementation:**
- `GET /tasks/{task_id}` → parse `output[]` array
- Filter for messages with `content[].type == "output_text"`
- Detect question patterns in last assistant message (same regex library as `manus_triage_task`)
- Return structured conversation + blocker summary

---

### Tool 2: `manus_task_unblock` *(Core Tool)*

**Purpose:** Manually reply to a blocked task by creating a context-aware continuation.

**Input:**
- `task_id: str` — the blocked task
- `answer: str` — your reply / the information the task needs
- `additional_context: str = ""` — optional extra context to inject
- `include_full_history: bool = True` — include full conversation in continuation prompt

**Output:**
- New continuation task ID and URL
- Summary of what was injected
- Memory entry stored (decision: "Unblocked {task_id} with: {answer[:80]}")

**Implementation:**
1. `GET /tasks/{task_id}` → read full `output[]`
2. Extract original user prompt (first user message)
3. Build conversation transcript (all messages)
4. Construct continuation prompt:
   ```
   CONTINUATION — Picking up from task {task_id}: {title}
   
   === ORIGINAL GOAL ===
   {first_user_message}
   
   === CONVERSATION HISTORY ===
   {full_transcript}
   
   === USER REPLY ===
   {answer}
   
   {additional_context if provided}
   
   === INSTRUCTIONS ===
   Continue from where the previous task left off. The user has provided the
   information above. Use it to complete the original goal.
   ```
5. `POST /tasks` with `parent_id={task_id}`, `prompt={continuation_prompt}`
6. Store in memory: handoff record (parent_id → child_id, answer summary)
7. Return new task ID + URL

---

### Tool 3: `manus_task_advance` *(Autonomous Unblock)*

**Purpose:** Autonomously unblock a task using memory, AI reasoning, and pattern matching.
No human input required — it figures out the answer itself.

**Input:**
- `task_id: str` — the blocked task
- `hint: str = ""` — optional hint to guide the search (e.g., "check Bitwarden for Namecheap")
- `dry_run: bool = False` — if True, shows what it would do without creating a task

**Output:**
- What it detected the task needs
- What context/answer it found (from memory, patterns, or AI reasoning)
- New continuation task ID (or dry-run summary)
- Confidence score (how sure it is the answer is correct)

**Implementation:**
1. `GET /tasks/{task_id}` → read conversation
2. Extract last assistant message → what is it asking for?
3. Run blocker classification (reuse `BLOCKER_PATTERNS` from `manus_triage_task`)
4. Based on blocker type, search memory:
   - `waiting_human` → `garza_learn(query=last_message[:200])`
   - `auth_*` → `garza_learn(query="credentials {service_name}")`
   - `missing_tool` → `garza_learn(query="MCP tool {tool_name}")`
5. If memory has relevant context → inject it as the answer
6. If no memory → use AI (Gemini/GPT) to reason about what the task needs based on conversation
7. Build continuation prompt via same logic as `manus_task_unblock`
8. Create continuation task (or dry-run)
9. Store: what was found, what was injected, confidence

**Confidence Scoring:**
- Memory hit with exact match → 0.9
- Memory hit with partial match → 0.7
- AI-reasoned answer → 0.5
- No answer found (fallback to generic continuation) → 0.2

---

### Tool 4: `manus_task_handoff_log`

**Purpose:** View the full chain of parent → child task handoffs.

**Input:**
- `task_id: str = ""` — if provided, show chain for this specific task; if empty, show all recent handoffs
- `limit: int = 20`

**Output:**
- Table of handoffs: parent_id → child_id, what was injected, timestamp, result
- Chain visualization for multi-hop tasks (A → B → C)

**Implementation:**
- Read from Fabric.so memory (tag: `task-handoff`)
- Filter by task_id if provided
- Format as readable chain

---

## Data Flow

```
User calls manus_task_advance("gkZjWcQrLLwz993EjYByEM")
    ↓
Read conversation from task
    ↓
Detect: task is asking for Bitwarden/Namecheap credentials
    ↓
garza_learn("Namecheap credentials Bitwarden login")
    ↓
Memory hit: "Namecheap API key: abc123, username: jgarza"
    ↓
Build continuation prompt with credentials injected
    ↓
POST /tasks with parent_id → new task ID
    ↓
Store handoff in memory
    ↓
Return: "Unblocked with Namecheap credentials from memory. New task: {id}"
```

---

## Error Handling

| Scenario | Behavior |
|----------|---------|
| Task not found | Return clear 404 error with task_id |
| Task has no conversation yet | Return "Task just started — no conversation to read yet" |
| Task is already completed | Return conversation + note that task is done |
| Memory search returns nothing | Fall back to AI reasoning, then generic continuation |
| Continuation task creation fails | Return error with original task state preserved |
| `dry_run=True` | Show full plan without creating any task |

---

## Memory Schema

Each handoff stored in Fabric.so as a `Decision` memory:

```json
{
  "content": "Task handoff: {parent_id} → {child_id}. Blocked on: {blocker_type}. Injected: {answer_summary}. Confidence: {score}",
  "tags": ["task-handoff", "garza-os", "task-advancement"],
  "importance": 0.8
}
```

---

## Success Criteria

1. `manus_task_read_conversation` returns full conversation for any task ID
2. `manus_task_unblock` creates a valid continuation task with full history + answer
3. `manus_task_advance` correctly identifies what a task needs and injects it from memory
4. `manus_task_handoff_log` shows all handoffs with parent→child chain
5. All 4 tools pass acceptance tests
6. Total tool count: 34 (30 existing + 4 new)

---

## YAGNI Cuts

The following were considered and **explicitly excluded** to keep scope tight:

- ~~Task branching (one parent → multiple children)~~ — not needed, linear chain is sufficient
- ~~Automatic retry loop (keep trying until task succeeds)~~ — too risky, human oversight needed
- ~~Task merging (combine two tasks into one)~~ — Manus API doesn't support this
- ~~Real-time streaming of task output~~ — SSE transport complexity not worth it
- ~~Credential vault integration~~ — memory already stores credentials; no separate vault needed

---

*Design approved. Proceed to writing-plans.*
