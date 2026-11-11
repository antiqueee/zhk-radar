FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY config ./config
COPY deploy/entrypoint.py ./deploy/entrypoint.py
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home radar && mkdir /workspace && chown radar:radar /workspace
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER radar
WORKDIR /workspace
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"
ENTRYPOINT ["python", "/app/deploy/entrypoint.py"]
CMD ["radar", "serve", "--host", "0.0.0.0", "--port", "8765"]
