# Build from the repo root:  docker build -f deploy/Dockerfile -t halcyon-sql-copilot .
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SQLAGENT_HOME=/app
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[kaggle,observability]"

COPY config ./config
COPY evals ./evals
COPY docs ./docs

RUN useradd --create-home --uid 10001 app && mkdir -p /app/data && chown -R app /app/data /app/evals
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"
CMD ["sqlagent-api", "--host", "0.0.0.0", "--port", "8000"]
