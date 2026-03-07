import logging
import sys
import os

# Configure logging FIRST before any other imports that might trigger config loading
logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(sys.stderr)])

# Dump LLM env vars immediately to diagnose Railway env var injection
logging.info("[ENV] LLM_BASE_URL=%s", os.getenv('LLM_BASE_URL', '<NOT SET>'))
logging.info("[ENV] LLM_MODEL=%s", os.getenv('LLM_MODEL', '<NOT SET>'))
logging.info("[ENV] LLM_API_KEY=%s", ('SET ('+str(len(os.getenv('LLM_API_KEY','')))+'chars)') if os.getenv('LLM_API_KEY') else '<NOT SET>')

import argparse
import asyncio
import contextlib
import atexit
import json
import os
from inspect import Parameter, Signature
from typing import Any, AsyncIterator, Dict, Optional

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

# ---------------------------------------------------------------------------
# NOTE: Heavy imports (app.logger, agents, tools) are deferred to avoid
# slowing down the Starlette/uvicorn startup. Railway's healthcheck fires
# within seconds of the process starting, so the /health route must be
# reachable before any LLM config or agent initialization occurs.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Auth Middleware
# ---------------------------------------------------------------------------

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """
    Validates auth token on the /mcp endpoint.
    Accepts token via:
      - X-MCP-Token: <token>  (preferred — Railway edge doesn't block custom headers)
      - Authorization: Bearer <token>  (standard, but Railway may block long hex tokens)
    If MCP_SERVER_AUTH_TOKEN is not set, auth is disabled (dev mode).
    """

    async def dispatch(self, request: Request, call_next):
        auth_token = os.getenv("MCP_SERVER_AUTH_TOKEN")

        # Only enforce auth on the MCP endpoint
        if auth_token and request.url.path.startswith("/api/v1"):
            # Check X-MCP-Token first (preferred — bypasses Railway edge 421 blocking)
            provided_token = request.headers.get("X-MCP-Token", "")
            if not provided_token:
                # Fall back to Authorization: Bearer <token>
                auth_header = request.headers.get("Authorization", "")
                parts = auth_header.split()
                if len(parts) == 2 and parts[0].lower() == "bearer":
                    provided_token = parts[1]

            if not provided_token:
                return JSONResponse(
                    {"error": "Missing auth token. Use X-MCP-Token header or Authorization: Bearer"}, status_code=401
                )
            if provided_token != auth_token:
                return JSONResponse(
                    {"error": "Invalid authentication credentials"}, status_code=401
                )

        return await call_next(request)


# ---------------------------------------------------------------------------
# Health Check — must be reachable IMMEDIATELY on startup
# ---------------------------------------------------------------------------

_server_ready = False

async def health_check(request: Request) -> JSONResponse:
    """Simple health endpoint for Railway's deployment checks."""
    return JSONResponse({
        "status": "ok",
        "service": "openmanus-mcp",
        "ready": _server_ready,
    })


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

class MCPServer:
    """
    OpenManus MCP Server.

    Exposes two layers of tools:
      1. High-level Agent Tools — run_manus, run_data_analysis, run_swe, run_browser
         Each spawns a fresh, isolated agent instance per call for full autonomy.
      2. Low-level Primitive Tools — bash, str_replace_editor, terminate
         Direct tool access for callers that need fine-grained control.

    Agent and tool imports are deferred until register_all_tools() is called
    so that the HTTP server (and /health endpoint) can start immediately.

    Transport: Streamable HTTP (MCP 2025-03-26 spec) via POST /mcp
    This transport works correctly through Railway's HTTP/2 edge proxy,
    unlike SSE which triggers 421 Misdirected Request errors.
    """

    # The MCP endpoint path. Using /api/v1 instead of /mcp because Railway's
    # edge proxy blocks ALL requests to /mcp (421 Invalid Host header).
    MCP_PATH = "/api/v1"

    def __init__(self, name: str = "openmanus"):
        port = int(os.getenv("PORT", os.getenv("FASTMCP_PORT", "8000")))
        self.server = FastMCP(name, port=port, streamable_http_path=self.MCP_PATH)
        self.tools: Dict[str, Any] = {}

    def _load_tools(self) -> None:
        """Deferred import and instantiation of all tools and agents."""
        import logging as _logging
        _log = _logging.getLogger(__name__)
        self._logger = _log

        from app.mcp.agent_tool import AgentTool

        # --- High-level: Agent-as-a-Tool (stateless, isolated per call) ---
        agent_map = {
            "run_manus": ("app.agent.manus", "Manus"),
            "run_data_analysis": ("app.agent.data_analysis", "DataAnalysis"),
            "run_swe": ("app.agent.swe", "SWEAgent"),
            "run_browser": ("app.agent.browser", "BrowserAgent"),
        }
        for tool_name, (module_path, class_name) in agent_map.items():
            try:
                import importlib
                mod = importlib.import_module(module_path)
                agent_class = getattr(mod, class_name)
                self.tools[tool_name] = AgentTool(agent_class=agent_class)
                _log.info(f"Registered agent tool: {tool_name}")
            except Exception as e:
                _log.warning(f"Skipping agent tool '{tool_name}': {e}")

        # --- Low-level: Primitive tools for direct control ---
        primitive_map = {
            "bash": ("app.tool.bash", "Bash"),
            "editor": ("app.tool.str_replace_editor", "StrReplaceEditor"),
            "terminate": ("app.tool.terminate", "Terminate"),
        }
        for tool_name, (module_path, class_name) in primitive_map.items():
            try:
                import importlib
                mod = importlib.import_module(module_path)
                tool_class = getattr(mod, class_name)
                self.tools[tool_name] = tool_class()
                _log.info(f"Registered primitive tool: {tool_name}")
            except Exception as e:
                _log.warning(f"Skipping primitive tool '{tool_name}': {e}")

        _log.info(f"Loaded {len(self.tools)} tools: {list(self.tools.keys())}")

    def register_tool(self, tool: Any, method_name: Optional[str] = None) -> None:
        """Register a tool with parameter validation and documentation."""
        logger = self._logger
        tool_name = method_name or tool.name
        tool_param = tool.to_param()
        tool_function = tool_param["function"]

        async def tool_method(**kwargs):
            logger.info(f"Executing {tool_name}: {kwargs}")
            result = await tool.execute(**kwargs)
            logger.info(f"Result of {tool_name}: {result}")
            if hasattr(result, "model_dump"):
                return json.dumps(result.model_dump())
            elif isinstance(result, dict):
                return json.dumps(result)
            return result

        tool_method.__name__ = tool_name
        tool_method.__doc__ = self._build_docstring(tool_function)
        tool_method.__signature__ = self._build_signature(tool_function)

        param_props = tool_function.get("parameters", {}).get("properties", {})
        required_params = tool_function.get("parameters", {}).get("required", [])
        tool_method._parameter_schema = {
            param_name: {
                "description": param_details.get("description", ""),
                "type": param_details.get("type", "any"),
                "required": param_name in required_params,
            }
            for param_name, param_details in param_props.items()
        }

        self.server.tool()(tool_method)
        logger.info(f"Registered tool: {tool_name}")

    def _build_docstring(self, tool_function: dict) -> str:
        description = tool_function.get("description", "")
        param_props = tool_function.get("parameters", {}).get("properties", {})
        required_params = tool_function.get("parameters", {}).get("required", [])
        docstring = description
        if param_props:
            docstring += "\n\nParameters:\n"
            for param_name, param_details in param_props.items():
                required_str = (
                    "(required)" if param_name in required_params else "(optional)"
                )
                param_type = param_details.get("type", "any")
                param_desc = param_details.get("description", "")
                docstring += (
                    f"    {param_name} ({param_type}) {required_str}: {param_desc}\n"
                )
        return docstring

    def _build_signature(self, tool_function: dict) -> Signature:
        param_props = tool_function.get("parameters", {}).get("properties", {})
        required_params = tool_function.get("parameters", {}).get("required", [])
        parameters = []
        for param_name, param_details in param_props.items():
            param_type = param_details.get("type", "")
            default = Parameter.empty if param_name in required_params else None
            annotation = Any
            if param_type == "string":
                annotation = str
            elif param_type == "integer":
                annotation = int
            elif param_type == "number":
                annotation = float
            elif param_type == "boolean":
                annotation = bool
            elif param_type == "object":
                annotation = dict
            elif param_type == "array":
                annotation = list
            param = Parameter(
                name=param_name,
                kind=Parameter.KEYWORD_ONLY,
                default=default,
                annotation=annotation,
            )
            parameters.append(param)
        return Signature(parameters=parameters)

    async def cleanup(self) -> None:
        if hasattr(self, "_logger"):
            self._logger.info("Cleaning up server resources")

    def register_all_tools(self) -> None:
        self._load_tools()
        for tool in self.tools.values():
            self.register_tool(tool)

    def build_app(self) -> Starlette:
        """
        Build a Starlette app using MCP Streamable HTTP transport (2025-03-26 spec).

        Key design:
        - The FastMCP streamable_http_app() has its own lifespan that initializes
          the StreamableHTTPSessionManager. We must run this lifespan.
        - We compose our outer Starlette with a lifespan that:
          1. Runs the FastMCP session manager (required for /mcp to work)
          2. Loads our tools in the background (so /health responds immediately)

        Transport: POST /mcp (Streamable HTTP)
        This works through Railway's HTTP/2 edge proxy.
        SSE (/sse) triggered 421 Misdirected Request from Railway's edge.
        """
        global _server_ready

        # Build the inner FastMCP Streamable HTTP app
        # (has /mcp route + session manager lifespan)
        fastmcp_app = self.server.streamable_http_app()

        # The session manager is now initialized inside fastmcp_app
        # We need to run it as part of our lifespan
        session_manager = self.server.session_manager

        @contextlib.asynccontextmanager
        async def lifespan(app: Starlette) -> AsyncIterator[None]:
            global _server_ready

            # Start the MCP session manager (required for Streamable HTTP)
            async with session_manager.run():
                # Load tools in background so /health responds immediately
                async def _load_tools_bg():
                    global _server_ready
                    await asyncio.sleep(0.5)
                    logging.info("Loading tools and agents in background...")
                    try:
                        from app.config import config as _cfg
                        for _name, _llm in _cfg.llm.items():
                            logging.info(f"[CONFIG] LLM[{_name}] base_url={_llm.base_url} model={_llm.model}")
                    except Exception as _ce:
                        logging.warning(f"[CONFIG] Could not read LLM config: {_ce}")
                    self.register_all_tools()
                    _server_ready = True
                    port = int(os.getenv("PORT", "8000"))
                    logging.info(
                        f"OpenManus MCP server ready\n"
                        f"  /health      — health check\n"
                        f"  {self.MCP_PATH}  — MCP Streamable HTTP endpoint (auth: {'enabled' if os.getenv('MCP_SERVER_AUTH_TOKEN') else 'disabled'})\n"
                        f"  Tools: {list(self.tools.keys())}"
                    )

                task = asyncio.create_task(_load_tools_bg())
                try:
                    yield
                finally:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        # Build the outer Starlette with health check + auth + MCP routes
        app = Starlette(
            debug=False,
            routes=[
                Route("/health", endpoint=health_check),
                Mount("/", app=fastmcp_app),
            ],
            middleware=[
                Middleware(BearerAuthMiddleware),
            ],
            lifespan=lifespan,
        )
        return app

    def run(self, transport: str = "stdio") -> None:
        """Run the MCP server in the specified transport mode."""
        atexit.register(lambda: asyncio.run(self.cleanup()))

        if transport == "sse":
            port = int(os.getenv("PORT", os.getenv("FASTMCP_PORT", "8000")))
            host = "0.0.0.0"
            app = self.build_app()
            logging.info(f"Starting uvicorn on {host}:{port} ...")
            uvicorn.run(app, host=host, port=port, log_level="info")
        else:
            # stdio mode — load everything synchronously (no healthcheck needed)
            self.register_all_tools()
            self.server.run(transport=transport)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenManus MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport mode: stdio (local) or sse (web/Railway)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    server = MCPServer()
    server.run(transport=args.transport)
