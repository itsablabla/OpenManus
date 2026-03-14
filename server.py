"""
OpenManus Web Server — Railway Deployment
Wraps OpenManus with a FastAPI REST API and simple web UI.
Configured to use CUDOS ASI Cloud inference endpoint.
"""
import asyncio
import os
import uuid
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# ── Runtime patch: fix DaytonaSettings BEFORE importing app.config ──
# Railway may serve a cached build where daytona_api_key is still required.
# We patch the source file in-place at container startup to make it optional.
import re as _re

def _patch_daytona_config():
    """Make DaytonaSettings.daytona_api_key optional at runtime."""
    config_py = os.path.join(os.path.dirname(__file__), "app", "config.py")
    if not os.path.exists(config_py):
        return
    with open(config_py, "r") as f:
        src = f.read()
    # Fix 1: make daytona_api_key optional
    src = src.replace(
        "    daytona_api_key: str\n",
        '    daytona_api_key: Optional[str] = Field(None, description="Daytona API key (optional)")\n'
    )
    # Fix 2: don't instantiate DaytonaSettings() with no args when no config
    src = src.replace(
        "            daytona_settings = DaytonaSettings()\n",
        "            daytona_settings = None\n"
    )
    with open(config_py, "w") as f:
        f.write(src)

_patch_daytona_config()

# Patch config to use environment variables for API key
def _patch_config():
    """Patch the config to use environment variables for API key."""
    config_path = os.path.join(os.path.dirname(__file__), "config", "config.toml")
    cudos_key = os.environ.get("CUDOS_API_KEY", "")
    if cudos_key and os.path.exists(config_path):
        with open(config_path, "r") as f:
            content = f.read()
        content = content.replace("CUDOS_API_KEY", cudos_key)
        with open(config_path, "w") as f:
            f.write(content)

_patch_config()

import app.config as _cfg_module

from app.agent.manus import Manus
from app.logger import logger

app = FastAPI(
    title="OpenManus API",
    description="OpenManus AI Agent — powered by CUDOS ASI Cloud",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory task store
tasks: dict = {}


class TaskRequest(BaseModel):
    prompt: str
    task_id: Optional[str] = None


class TaskResponse(BaseModel):
    task_id: str
    status: str
    prompt: str
    result: Optional[str] = None
    error: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None


async def run_agent_task(task_id: str, prompt: str):
    """Run OpenManus agent in background."""
    tasks[task_id]["status"] = "running"
    try:
        agent = await Manus.create()
        # Capture output
        import io
        from contextlib import redirect_stdout
        output = io.StringIO()
        try:
            await agent.run(prompt)
            result = f"Task completed successfully. Check workspace directory for outputs."
        except Exception as e:
            result = f"Agent error: {str(e)}"
        finally:
            await agent.cleanup()
        tasks[task_id]["status"] = "completed"
        tasks[task_id]["result"] = result
        tasks[task_id]["completed_at"] = datetime.utcnow().isoformat()
    except Exception as e:
        tasks[task_id]["status"] = "failed"
        tasks[task_id]["error"] = str(e)
        tasks[task_id]["completed_at"] = datetime.utcnow().isoformat()
        logger.error(f"Task {task_id} failed: {e}")


@app.get("/", response_class=HTMLResponse)
async def root():
    """Simple web UI for OpenManus."""
    return HTMLResponse(content="""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OpenManus — GARZA OS</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f0f0f; color: #e0e0e0; min-height: 100vh; }
        .container { max-width: 900px; margin: 0 auto; padding: 40px 20px; }
        h1 { font-size: 2rem; font-weight: 700; color: #fff; margin-bottom: 8px; }
        .subtitle { color: #888; margin-bottom: 40px; font-size: 0.95rem; }
        .badge { display: inline-block; background: #1a1a2e; border: 1px solid #333; border-radius: 20px; padding: 4px 12px; font-size: 0.8rem; color: #7c8cf8; margin-bottom: 30px; }
        .card { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 12px; padding: 24px; margin-bottom: 24px; }
        textarea { width: 100%; background: #111; border: 1px solid #333; border-radius: 8px; padding: 14px; color: #e0e0e0; font-size: 0.95rem; resize: vertical; min-height: 120px; outline: none; font-family: inherit; }
        textarea:focus { border-color: #7c8cf8; }
        button { background: #7c8cf8; color: #fff; border: none; border-radius: 8px; padding: 12px 28px; font-size: 0.95rem; font-weight: 600; cursor: pointer; transition: background 0.2s; }
        button:hover { background: #6b7cf0; }
        button:disabled { background: #444; cursor: not-allowed; }
        .task-list { margin-top: 8px; }
        .task-item { background: #111; border: 1px solid #2a2a2a; border-radius: 8px; padding: 16px; margin-bottom: 12px; }
        .task-id { font-family: monospace; font-size: 0.8rem; color: #666; }
        .task-prompt { margin: 8px 0; font-size: 0.9rem; }
        .status { display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 0.8rem; font-weight: 600; }
        .status.pending { background: #2a2a00; color: #ffd700; }
        .status.running { background: #002a2a; color: #00d4aa; }
        .status.completed { background: #002a00; color: #00d400; }
        .status.failed { background: #2a0000; color: #ff4444; }
        .result { margin-top: 10px; padding: 10px; background: #0a0a0a; border-radius: 6px; font-size: 0.85rem; color: #aaa; white-space: pre-wrap; }
        .api-section { margin-top: 8px; }
        code { background: #111; padding: 2px 6px; border-radius: 4px; font-family: monospace; font-size: 0.85rem; color: #7c8cf8; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 OpenManus</h1>
        <p class="subtitle">AI Agent powered by CUDOS ASI Cloud · GARZA OS</p>
        <div class="badge">⚡ meta-llama/llama-3.3-70b-instruct via CUDOS</div>

        <div class="card">
            <h2 style="margin-bottom:16px;font-size:1.1rem;">Run a Task</h2>
            <textarea id="prompt" placeholder="Enter your task for OpenManus... e.g. 'Research the latest AI news and summarize the top 5 stories'"></textarea>
            <br><br>
            <button onclick="submitTask()" id="submitBtn">▶ Run Task</button>
        </div>

        <div class="card">
            <h2 style="margin-bottom:16px;font-size:1.1rem;">Tasks</h2>
            <div class="task-list" id="taskList">
                <p style="color:#555;font-size:0.9rem;">No tasks yet. Submit a prompt above to get started.</p>
            </div>
        </div>

        <div class="card api-section">
            <h2 style="margin-bottom:12px;font-size:1.1rem;">REST API</h2>
            <p style="color:#888;font-size:0.9rem;margin-bottom:10px;">Use the API directly for programmatic access:</p>
            <p><code>POST /tasks</code> — Submit a task with <code>{"prompt": "..."}</code></p>
            <p style="margin-top:8px;"><code>GET /tasks/{task_id}</code> — Get task status and result</p>
            <p style="margin-top:8px;"><code>GET /tasks</code> — List all tasks</p>
            <p style="margin-top:8px;"><code>GET /health</code> — Health check</p>
            <p style="margin-top:8px;"><a href="/docs" style="color:#7c8cf8;">📖 Interactive API Docs (Swagger)</a></p>
        </div>
    </div>

    <script>
        let pollingIntervals = {};

        async function submitTask() {
            const prompt = document.getElementById('prompt').value.trim();
            if (!prompt) return alert('Please enter a prompt');
            
            const btn = document.getElementById('submitBtn');
            btn.disabled = true;
            btn.textContent = '⏳ Submitting...';

            try {
                const res = await fetch('/tasks', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({prompt})
                });
                const task = await res.json();
                document.getElementById('prompt').value = '';
                renderTask(task);
                pollTask(task.task_id);
            } catch(e) {
                alert('Error: ' + e.message);
            } finally {
                btn.disabled = false;
                btn.textContent = '▶ Run Task';
            }
        }

        function renderTask(task) {
            const list = document.getElementById('taskList');
            const existing = document.getElementById('task-' + task.task_id);
            const html = `
                <div class="task-item" id="task-${task.task_id}">
                    <div class="task-id">${task.task_id}</div>
                    <div class="task-prompt">${task.prompt}</div>
                    <span class="status ${task.status}">${task.status}</span>
                    ${task.result ? '<div class="result">' + task.result + '</div>' : ''}
                    ${task.error ? '<div class="result" style="color:#ff6666;">' + task.error + '</div>' : ''}
                </div>`;
            if (existing) {
                existing.outerHTML = html;
            } else {
                if (list.querySelector('p')) list.innerHTML = '';
                list.insertAdjacentHTML('afterbegin', html);
            }
        }

        function pollTask(taskId) {
            if (pollingIntervals[taskId]) return;
            pollingIntervals[taskId] = setInterval(async () => {
                try {
                    const res = await fetch('/tasks/' + taskId);
                    const task = await res.json();
                    renderTask(task);
                    if (task.status === 'completed' || task.status === 'failed') {
                        clearInterval(pollingIntervals[taskId]);
                        delete pollingIntervals[taskId];
                    }
                } catch(e) {}
            }, 3000);
        }

        // Load existing tasks on page load
        fetch('/tasks').then(r => r.json()).then(tasks => {
            if (tasks.length > 0) {
                document.getElementById('taskList').innerHTML = '';
                tasks.forEach(t => { renderTask(t); if (t.status === 'running' || t.status === 'pending') pollTask(t.task_id); });
            }
        });
    </script>
</body>
</html>
""")


@app.post("/tasks", response_model=TaskResponse)
async def create_task(request: TaskRequest, background_tasks: BackgroundTasks):
    """Submit a new task to OpenManus."""
    task_id = request.task_id or str(uuid.uuid4())
    task = {
        "task_id": task_id,
        "status": "pending",
        "prompt": request.prompt,
        "result": None,
        "error": None,
        "created_at": datetime.utcnow().isoformat(),
        "completed_at": None,
    }
    tasks[task_id] = task
    background_tasks.add_task(run_agent_task, task_id, request.prompt)
    return TaskResponse(**task)


@app.get("/tasks", response_model=list[TaskResponse])
async def list_tasks():
    """List all tasks."""
    return [TaskResponse(**t) for t in tasks.values()]


@app.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    """Get task status and result."""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskResponse(**tasks[task_id])


@app.get("/health")
async def health():
    """Health check endpoint."""
    cudos_key = os.environ.get("CUDOS_API_KEY", "")
    return {
        "status": "healthy",
        "service": "OpenManus",
        "model": "meta-llama/llama-3.3-70b-instruct",
        "inference_provider": "CUDOS ASI Cloud",
        "api_key_configured": bool(cudos_key),
        "timestamp": datetime.utcnow().isoformat()
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
