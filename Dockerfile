FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
COPY deploy/playwright-preload/ /opt/playwright/
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 && /app/.venv/bin/playwright install --with-deps --only-shell chromium && chmod -R a+rX /opt/playwright && rm -rf /var/lib/apt/lists/*
COPY src ./src
COPY config ./config
COPY deploy/entrypoint.py ./deploy/entrypoint.py
RUN uv sync --frozen --no-dev && chmod -R a+rX /app/src /app/config && useradd --uid 10001 --create-home radar && mkdir /workspace && chown radar:radar /workspace
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER radar
WORKDIR /workspace
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"
ENTRYPOINT ["python", "/app/deploy/entrypoint.py"]
CMD ["radar", "serve", "--host", "0.0.0.0", "--port", "8765"]
