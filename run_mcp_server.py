# coding: utf-8
"""
Entry point for the OpenManus Hybrid MCP Server.
Launches the FastMCP server using SSE transport.
Disables DNS rebinding protection so Railway's proxy Host header is accepted.
Registers BearerAuthMiddleware for Fix 5 (auth enforcement).
"""
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", os.environ.get("FASTMCP_PORT", "8080")))
HOST = "0.0.0.0"

logger.info(f"[run_mcp_server] Starting SSE on {HOST}:{PORT}")

from app.mcp.server import mcp, BearerAuthMiddleware

# Override host/port settings
mcp.settings.host = HOST
mcp.settings.port = PORT

# Disable DNS rebinding protection — Railway's edge proxy handles security.
try:
    from mcp.server.transport_security import TransportSecuritySettings
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
    logger.info("DNS rebinding protection disabled (Railway edge handles security)")
except (ImportError, AttributeError):
    logger.info("TransportSecuritySettings not available — skipping")

# Fix 5 — Register auth middleware on the underlying Starlette app
# Must be done after mcp.sse_app() is built, so we build it first
app = mcp.sse_app()
app.add_middleware(BearerAuthMiddleware)

# Add /health endpoint for Railway healthcheck (must bypass auth middleware)
# Insert as a raw Starlette route so it's checked before the middleware chain
from starlette.routing import Route
from starlette.responses import JSONResponse as _JSONResponse


async def _health_handler(request):
    return _JSONResponse({"status": "ok", "service": "OpenManus MCP"})


app.routes.insert(0, Route("/health", _health_handler, methods=["GET"]))
logger.info("Registered /health endpoint for Railway healthcheck")

logger.info(f"Starting MCP SSE server on {HOST}:{PORT} with Bearer auth enforcement")

import uvicorn
uvicorn.run(app, host=HOST, port=PORT, h11_max_incomplete_event_size=16384)
