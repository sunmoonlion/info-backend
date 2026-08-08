ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE} AS builder

ARG PYPI_INDEX_URL=https://pypi.org/simple
ARG UV_VERSION=0.11.32
ENV PIP_INDEX_URL=${PYPI_INDEX_URL} \
    UV_DEFAULT_INDEX=${PYPI_INDEX_URL} \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install "uv==${UV_VERSION}"
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --frozen
COPY . .

FROM ${PYTHON_IMAGE} AS runtime

RUN groupadd --system --gid 1001 appgroup \
    && useradd --system --uid 1001 --gid appgroup \
       --home-dir /app --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY --from=builder --chown=appuser:appgroup /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER appuser
EXPOSE 8000

CMD ["uvicorn", "app.bootstrap.api:app", "--host", "0.0.0.0", "--port", "8000"]
