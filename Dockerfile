# Front-desk voice agent (front_desk.server) for Cloud Run.
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    OTEL_SDK_DISABLED=true \
    GOOGLE_GENAI_USE_VERTEXAI=FALSE

WORKDIR /app

# Dependencies first so code changes don't bust the layer cache.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY front_desk/ front_desk/
COPY assets/clinic/ assets/clinic/

RUN useradd --create-home --uid 1000 app && chown -R app /app
USER app

# Cloud Run injects PORT (default 8080).
ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn front_desk.server:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
