import logging
import sys

logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(sys.stderr)])

import argparse
import asyncio
import atexit
import json
import os
from inspect import Parameter, Signature
from typing import Any, Dict, Optional

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from app.logger import logger
from app.tool.base import BaseTool
from app.tool.bash import Bash
from app.tool.browser_use_tool import BrowserUseTool
from app.tool.str_replace_editor import StrReplaceEditor
from app.tool.terminate import Terminate

# Agent imports for the high-level agent tools
from app.agent.manus import Manus
from app.agent.data_analysis import DataAnalysis
from app.agent.swe import SWEAgent
from app.agent.browser import BrowserAgent
from app.mcp.agent_tool import AgentTool


# ---------------------------------------------------------------------------
# Auth Middleware
# ---------------------------------------------------------------------------

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """
    Validates Bearer token on the /sse endpoint.
    If MCP_SERVER_AUTH_TOKEN is not set, auth is disabled (dev mode).
    """

    async def dispatch(self, request: Request, call_next):
        auth_token = os.getenv("MCP_SERVER_AUTH_TOKEN")

        # Only enforce auth if the token is configured
        if auth_token and request.url.path == "/sse":
            auth_header = request.headers.get("Authorization", "")
            if not auth_header:
                return JSONResponse(
                    {"error": "Missing Authorization header"}, status_code=401
                )
            parts = auth_header.split()
            if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != auth_token:
                return JSONResponse(
                    {"error": "Invalid authentication credentials"}, status_code=401
                )

        return await call_next(request)


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------

async def health_check(request: Request) -> JSONResponse:
    """Simple health endpoint for Railway's deployment checks."""
    return JSONResponse({"status": "ok", "service": "openmanus-mcp"})


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
    """

    def __init__(self, name: str = "openmanus"):
        port = int(os.getenv("PORT", os.getenv("FASTMCP_PORT", "8000")))
        self.server = FastMCP(name, port=port)
        self.tools: Dict[str, BaseTool] = {}

        # --- High-level: Agent-as-a-Tool (stateless, isolated per call) ---
        self.tools["run_manus"] = AgentTool(agent_class=Manus)
        self.tools["run_data_analysis"] = AgentTool(agent_class=DataAnalysis)
        self.tools["run_swe"] = AgentTool(agent_class=SWEAgent)
        self.tools["run_browser"] = AgentTool(agent_class=BrowserAgent)

        # --- Low-level: Primitive tools for direct control ---
        self.tools["bash"] = Bash()
        self.tools["editor"] = StrReplaceEditor()
        self.tools["terminate"] = Terminate()

    def register_tool(self, tool: BaseTool, method_name: Optional[str] = None) -> None:
        """Register a tool with parameter validation and documentation."""
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
        logger.info("Cleaning up server resources")

    def register_all_tools(self) -> None:
        for tool in self.tools.values():
            self.register_tool(tool)

    def _build_sse_app(self) -> Starlette:
        """
        Build a Starlette app wrapping FastMCP's SSE transport,
        adding the health check route and Bearer auth middleware.
        """
        # Get the raw Starlette app from FastMCP (has /sse and /messages/ routes)
        fastmcp_app = self.server.sse_app()

        # Wrap it with our health check and auth middleware
        app = Starlette(
            debug=False,
            routes=[
                Route("/health", endpoint=health_check),
                Mount("/", app=fastmcp_app),
            ],
            middleware=[
                Middleware(BearerAuthMiddleware),
            ],
        )
        return app

    def run(self, transport: str = "stdio") -> None:
        """Run the MCP server in the specified transport mode."""
        self.register_all_tools()
        atexit.register(lambda: asyncio.run(self.cleanup()))

        logger.info(f"Starting OpenManus MCP server ({transport} mode)")

        if transport == "sse":
            port = int(os.getenv("PORT", os.getenv("FASTMCP_PORT", "8000")))
            # Always bind to 0.0.0.0 in SSE mode so Railway's load balancer can reach us.
            # FastMCP defaults to 127.0.0.1 which is unreachable from outside the container.
            host = "0.0.0.0"
            logger.info(f"SSE server listening on {host}:{port}")
            logger.info(f"  /health  — health check")
            logger.info(f"  /sse     — MCP endpoint (auth: {'enabled' if os.getenv('MCP_SERVER_AUTH_TOKEN') else 'disabled'})")
            logger.info(f"  Tools registered: {list(self.tools.keys())}")
            app = self._build_sse_app()
            uvicorn.run(app, host=host, port=port, log_level="info")
        else:
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
