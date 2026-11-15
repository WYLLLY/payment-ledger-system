FROM python:3.14.10-alpine3.24@sha256:5862d971d1f0ae26b258cefa4e893556b1d3aba81bd2ff30888704ad378bab67 AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-build
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.14.10-alpine3.24@sha256:5862d971d1f0ae26b258cefa4e893556b1d3aba81bd2ff30888704ad378bab67 AS runtime
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN /usr/local/bin/python -m pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python3.14/ensurepip \
    && addgroup -S -g 10001 app \
    && adduser -S -D -H -u 10001 -G app app \
    && mkdir /data \
    && chown app:app /data
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts/provision_app.py ./scripts/provision_app.py
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "payment_ledger.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log"]
