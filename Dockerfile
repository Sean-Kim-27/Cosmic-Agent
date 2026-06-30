FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    XDG_DATA_HOME=/data

WORKDIR /app

RUN groupadd --system --gid 1001 cosmic \
    && useradd --system --uid 1001 --gid cosmic --home-dir /app cosmic \
    && mkdir -p /data \
    && chown -R cosmic:cosmic /app /data

COPY --chown=cosmic:cosmic pyproject.toml README.md ./
COPY --chown=cosmic:cosmic app ./app

RUN python -m pip install --upgrade pip \
    && python -m pip install .

USER cosmic

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/openapi.json', timeout=3).read()" || exit 1

CMD ["uvicorn", "app.api.application:app", "--host", "0.0.0.0", "--port", "8000"]
