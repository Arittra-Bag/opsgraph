# Python 3.11 slim multi-architecture manifest, pinned for reproducible releases.
FROM python:3.11-slim@sha256:a630a63cdb314e2d138a2fca3e375e319e8568346ffafac5b980f888630ac4f1

ARG OPSGRAPH_VERSION=0.0.0-dev
ARG OPSGRAPH_REVISION=unknown
ARG OPSGRAPH_CREATED=1970-01-01T00:00:00Z
LABEL org.opencontainers.image.title="OpsGraph" \
      org.opencontainers.image.description="Evidence-first, read-only PostgreSQL investigations" \
      org.opencontainers.image.url="https://opsgraph-site-seven.vercel.app/" \
      org.opencontainers.image.source="https://github.com/Arittra-Bag/opsgraph" \
      org.opencontainers.image.documentation="https://github.com/Arittra-Bag/opsgraph#readme" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${OPSGRAPH_VERSION}" \
      org.opencontainers.image.revision="${OPSGRAPH_REVISION}" \
      org.opencontainers.image.created="${OPSGRAPH_CREATED}"

# Keep the control-plane state on the writable mounted volume. The image itself
# remains read-only at runtime.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPSGRAPH_STATE_PATH=/data/state.db
WORKDIR /app
RUN useradd --create-home --uid 10001 opsgraph
COPY pyproject.toml README.md ./
COPY requirements.lock ./
COPY src ./src
COPY skillpacks ./skillpacks
COPY policies ./policies
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && \
    pip install --no-cache-dir --no-deps .
RUN mkdir -p /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import json,urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/ready', timeout=2))['ready']"
CMD ["uvicorn", "opsgraph.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
