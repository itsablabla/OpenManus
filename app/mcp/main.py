from app.mcp.server import MCPServer

server = MCPServer()
app = server._build_sse_app()

@app.on_event("startup")
async def startup_event():
    await server._startup()
