# coding: utf-8
"""
Entry point for the OpenManus Hybrid MCP Server.
Launches the FastMCP server using SSE transport so Railway can serve
HTTP connections from Claude Desktop and other MCP clients.

Explicitly sets host=0.0.0.0 and port from PORT env var (Railway standard)
to ensure the server binds on the public interface regardless of mcp version.
"""
import os

if __name__ == "__main__":
    # Import the FastMCP app from the hybrid server module
    from app.mcp.server import mcp

    # Override host/port settings directly on the mcp object.
    # Railway injects PORT env var; FastMCP defaults to 127.0.0.1:8000 which
    # is unreachable from outside the container.
    port = int(os.environ.get("PORT", os.environ.get("FASTMCP_PORT", "8080")))
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = port

    print(f"[run_mcp_server] Starting SSE on 0.0.0.0:{port}", flush=True)
    mcp.run(transport="sse")
