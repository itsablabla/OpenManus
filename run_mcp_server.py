# coding: utf-8
"""
Entry point for the OpenManus Hybrid MCP Server.
Launches the FastMCP server using SSE transport so Railway can serve
HTTP connections from Claude Desktop and other MCP clients.
"""
import argparse
import sys

def parse_args():
    parser = argparse.ArgumentParser(description="OpenManus Hybrid MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="sse",
        help="Transport protocol (default: sse)",
    )
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    # Import here to avoid circular imports during module load
    from app.mcp.server import mcp
    mcp.run(transport=args.transport)
