FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/balakrishnagatla/llm-cost-guard-platform" \
      org.opencontainers.image.description="LLM Cost Guard: LLM cost gateway with routing, caching, budgets and tracing" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

# Pick up OS security fixes released since the base image was built.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run as an unprivileged user, not root.
RUN useradd --create-home --uid 10001 costguard && mkdir -p /srv/data && chown costguard /srv/data
COPY --chown=root:root app ./app
COPY --chown=root:root scripts ./scripts
COPY --chown=root:root pricing.json routing.json ./

USER 10001
ENV DB_PATH=/srv/data/costguard.db
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=2)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header", "--timeout-graceful-shutdown", "20"]
