FROM python:3.13-slim AS builder

WORKDIR /build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.13-slim

WORKDIR /app

RUN groupadd --system --gid 1000 mcp \
    && useradd --system --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin mcp

COPY --from=builder /install /usr/local

USER mcp

EXPOSE 3702

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD ["python", "-m", "mcp_searxng.healthcheck"]

CMD ["mcp-searxng"]
