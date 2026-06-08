# syntax=docker/dockerfile:1.7

# ---- Stage 1: builder ----
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY pyproject.toml README.md LICENSE /build/
COPY src /build/src

RUN pip install --upgrade pip build \
 && python -m build --wheel --outdir /build/dist

# ---- Stage 2: runtime ----
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    SOLALPHA_DATA_DIR=/var/lib/solalpha \
    SOLALPHA_LOG_DIR=/var/log/solalpha \
    SOLALPHA_PROFILE=paper

RUN groupadd --system --gid 1000 solalpha \
 && useradd --system --gid 1000 --uid 1000 --home-dir /home/solalpha --create-home solalpha \
 && mkdir -p /var/lib/solalpha /var/log/solalpha /etc/solalpha \
 && chown -R solalpha:solalpha /var/lib/solalpha /var/log/solalpha /etc/solalpha

COPY --from=builder /build/dist/*.whl /tmp/

RUN pip install /tmp/*.whl \
 && rm -f /tmp/*.whl

COPY config /etc/solalpha/config

USER solalpha
WORKDIR /home/solalpha

EXPOSE 9464

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9464/health', timeout=3).status == 200 else 1)"]

ENTRYPOINT ["solalpha"]
CMD ["paper", "--config-dir", "/etc/solalpha/config"]
