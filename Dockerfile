FROM python:3.12-slim

WORKDIR /app/OpenManus

# Install system deps including Playwright browser requirements
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl wget gnupg ca-certificates \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libdbus-1-3 libexpat1 libxcb1 libxkbcommon0 libx11-6 \
    libxcomposite1 libxdamage1 libxext6 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && (command -v uv >/dev/null 2>&1 || pip install --no-cache-dir uv)

COPY . .

# Install Python dependencies
RUN uv pip install --system -r requirements.txt

# Install Daytona stub package (satisfies imports without real Daytona SDK)
# The real Daytona SDK is not on PyPI; this stub allows all agents to load.
# Sandbox operations will raise NotImplementedError if called without a real key.
RUN pip install --no-cache-dir ./daytona_stub/

# Install Playwright Chromium browser binaries
RUN playwright install chromium

# Create workspace directory for agent file output (Railway Volume mounts here)
RUN mkdir -p /app/OpenManus/workspace

# Expose default port (Railway overrides via $PORT env var at runtime)
EXPOSE 8000

# Start the MCP server in SSE (web) mode
CMD ["uvicorn", "app.mcp.main:app", "--host", "0.0.0.0", "--port", "8000"]
