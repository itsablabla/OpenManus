# coding: utf-8
"""
Entry point for the OpenManus Hybrid MCP Server.
Launches the FastMCP server using SSE transport.
Disables DNS rebinding protection so Railway's proxy Host header is accepted.
"""
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", os.environ.get("FASTMCP_PORT", "8080")))
HOST = "0.0.0.0"

logger.info(f"[run_mcp_server] Starting SSE on {HOST}:{PORT}")

from app.mcp.server import mcp

# Override host/port settings
mcp.settings.host = HOST
mcp.settings.port = PORT

# Disable DNS rebinding protection — Railway's edge proxy handles security.
# Without this, the mcp TransportSecurityMiddleware returns 421 for Railway's Host header.
try:
    from mcp.server.transport_security import TransportSecuritySettings
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
    logger.info("DNS rebinding protection disabled (Railway edge handles security)")
except (ImportError, AttributeError):
    logger.info("TransportSecuritySettings not available — skipping")

logger.info(f"Starting MCP SSE server on {HOST}:{PORT}")
mcp.run(transport="sse")
