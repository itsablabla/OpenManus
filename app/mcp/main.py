# Uvicorn entrypoint for the OpenManus MCP server.
# Uses MCP Streamable HTTP transport (POST /mcp) — works through Railway's HTTP/2 proxy.
# SSE transport (/sse) triggered 421 Misdirected Request from Railway's edge.

from app.mcp.server import MCPServer

_server = MCPServer()
app = _server.build_app()
