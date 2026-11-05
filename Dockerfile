FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SQLAGENT_HOME=/app
WORKDIR /app

COPY pyproject.toml ./
COPY sqlagent ./sqlagent
RUN pip install --no-cache-dir ".[kaggle,observability]"

COPY config ./config
COPY setup ./setup
COPY eval ./eval

RUN useradd --create-home --uid 10001 app && mkdir -p /app/data && chown -R app /app/data
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"
CMD ["uvicorn", "sqlagent.api:app", "--host", "0.0.0.0", "--port", "8000"]
