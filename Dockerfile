FROM node:22-bookworm-slim AS frontend
WORKDIR /build/web-client
COPY web-client/package.json web-client/package-lock.json ./
RUN npm ci
COPY web-client/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS python-build
WORKDIR /app
RUN pip install --no-cache-dir uv==0.9.5
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
ARG WITH_OCR=false
RUN if [ "$WITH_OCR" = "true" ]; then uv sync --frozen --no-dev --extra web --extra cloud --extra xlsx --extra ocr; else uv sync --frozen --no-dev --extra web --extra cloud --extra xlsx; fi

FROM python:3.12-slim-bookworm AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 lit && useradd --uid 10001 --gid lit --no-create-home lit
WORKDIR /app
COPY --from=python-build /app /app
COPY --from=frontend /build/web-client/dist /app/src/litworkbench/web/static
COPY scripts/migrate_cloud_workspace.py /app/scripts/migrate_cloud_workspace.py
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "litworkbench.web.cloud", "--workspace", "/data", "--host", "0.0.0.0", "--port", "8000"]
