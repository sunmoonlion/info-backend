FROM python:3.12-slim

ARG PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PYPI_INDEX_URL} \
    UV_DEFAULT_INDEX=${PYPI_INDEX_URL}

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN pip install uv --no-cache-dir && uv sync --no-dev --frozen

COPY . .

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
