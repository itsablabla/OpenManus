from app.mcp.server import MCPServer
from starlette.middleware.trustedhost import TrustedHostMiddleware

server = MCPServer()
app = server._build_sse_app()
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*.up.railway.app"])

@app.on_event("startup")
async def startup_event():
    await server._startup()
