FROM python:3.12-slim
WORKDIR /app/OpenManus
# Cache bust: force full rebuild when this changes
ARG BUILD_DATE=2026-03-07-v4
RUN apt-get update && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/* \
    && (command -v uv >/dev/null 2>&1 || pip install --no-cache-dir uv)
COPY . .
RUN uv pip install --system -r requirements.txt
RUN chmod +x entrypoint.sh
CMD ["./entrypoint.sh"]
