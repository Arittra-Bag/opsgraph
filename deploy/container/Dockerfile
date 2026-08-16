# Python 3.11 slim multi-architecture manifest, pinned for reproducible releases.
FROM python:3.11-slim@sha256:a630a63cdb314e2d138a2fca3e375e319e8568346ffafac5b980f888630ac4f1

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
  CMD python -c "import json,urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2))['ok']"
CMD ["uvicorn", "opsgraph.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
