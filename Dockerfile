FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.14

WORKDIR /app

# uv gives us fast, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
# Pin explicitly to the image's own interpreter -- otherwise uv's python
# discovery can pick up an unrelated interpreter path from the build env.
RUN uv sync --frozen --no-dev --python /usr/local/bin/python3.14

COPY . .

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000

CMD ["uvicorn", "web.app:api", "--host", "0.0.0.0", "--port", "8000"]
