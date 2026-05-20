ARG PYTHON_VERSION=3.12
# Docker 镜像仓库；CI 传 Harbor 地址，本地默认同 Harbor（内网可用）；纯外网传 --build-arg REGISTRY= 退回 DockerHub
ARG REGISTRY=harbor.sunmoonai.com/k8s-images

# ============================================================================
# Stage 1: Type-check — 安装所有依赖（含 dev），运行 pyright 类型检查
# ============================================================================
FROM ${REGISTRY}/python:${PYTHON_VERSION}-slim AS type-check

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 && rm -rf /var/lib/apt/lists/*
RUN pip install uv --no-cache-dir

COPY app/pyproject.toml app/uv.lock ./
# 安装全量依赖（含 dev），确保 pyright 与业务依赖均可用
RUN uv sync --frozen

COPY app/ .

# 运行 pyright 类型检查；失败则 Docker build 中断
RUN uv run pyright

# ============================================================================
# Stage 2: Builder — 在 type-check 基础上裁剪 dev 依赖，生成精简 .venv
# 继承 type-check 确保 pyright 必须通过才能进入后续阶段
# ============================================================================
FROM type-check AS builder

RUN uv sync --no-dev --frozen

# ============================================================================
# Stage 3: Runtime — 最小化运行镜像
# ============================================================================
ARG PYTHON_VERSION=3.12
ARG REGISTRY=harbor.sunmoonai.com/k8s-images
FROM ${REGISTRY}/python:${PYTHON_VERSION}-slim AS run

WORKDIR /app

COPY app/ ./
COPY --from=builder /app/.venv ./.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
