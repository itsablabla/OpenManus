# This module is the uvicorn entrypoint for the OpenManus MCP server.
# It uses MCP Streamable HTTP transport (POST /mcp) which works correctly
# through Railway's HTTP/2 edge proxy.
#
# The previous SSE transport (/sse) triggered 421 Misdirected Request errors
# from Railway's edge because HTTP/2 multiplexing is incompatible with
# long-lived SSE streams in this proxy configuration.

from app.mcp.server import MCPServer

_server = MCPServer()
app = _server._build_streamable_http_app()


@app.on_event("startup")
async def startup_event():
    import asyncio
    import logging
    import os

    global _server_ready

    await asyncio.sleep(0.5)
    logging.info("Loading tools and agents in background...")

    try:
        from app.config import config as _cfg
        for _name, _llm in _cfg.llm.items():
            logging.info(f"[CONFIG] LLM[{_name}] base_url={_llm.base_url} model={_llm.model}")
    except Exception as _ce:
        logging.warning(f"[CONFIG] Could not read LLM config: {_ce}")

    _server.register_all_tools()

    from app.mcp import server as _srv_module
    _srv_module._server_ready = True

    port = int(os.getenv("PORT", "8000"))
    logging.info(
        f"OpenManus MCP server ready\n"
        f"  /health  — health check\n"
        f"  /mcp     — MCP Streamable HTTP endpoint (auth: {'enabled' if os.getenv('MCP_SERVER_AUTH_TOKEN') else 'disabled'})\n"
        f"  Tools: {list(_server.tools.keys())}"
    )
