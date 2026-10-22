# The reconciliation service. The index is not in the image: it lives on a
# volume at /data, and is fetched there the first time the service starts
# (see docker/entrypoint.sh).
#
#   docker compose up
#
# or by hand:
#
#   docker build -t lc-reconcile .
#   docker run -p 8000:8000 -v lc-reconcile:/data lc-reconcile

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --extra server --no-editable

FROM python:3.13-slim-bookworm
RUN useradd --system --create-home --home-dir /home/lc lc \
    && mkdir /data && chown lc /data
COPY --from=build /app/.venv /app/.venv
COPY docker/entrypoint.sh /usr/local/bin/entrypoint
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    LC_RECONCILE_DB=/data/lc-reconcile.sqlite
USER lc
WORKDIR /data
VOLUME /data
EXPOSE 8000
# Fetching the index on first start takes a while, hence the long start period.
HEALTHCHECK --interval=30s --timeout=5s --start-period=1h \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/status', timeout=4)"]
ENTRYPOINT ["entrypoint"]
CMD ["serve", "--host", "0.0.0.0", "--sync-every", "24"]
