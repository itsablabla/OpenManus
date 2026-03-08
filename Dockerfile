FROM python:3.12-slim
WORKDIR /app/OpenManus
ARG BUILD_DATE=2026-03-07-v8
RUN apt-get update && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/* \
    && (command -v uv >/dev/null 2>&1 || pip install --no-cache-dir uv)
# Cache bust: BUILD_DATE must be used in a RUN command to invalidate COPY layer
RUN echo "Build date: ${BUILD_DATE}"
COPY . .
RUN uv pip install --system -r requirements.txt
RUN chmod +x entrypoint.sh
CMD ["./entrypoint.sh"]
