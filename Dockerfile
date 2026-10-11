# The tutor in one container: Python 3.12, the locked packages without the dev tools, one uvicorn worker.
FROM python:3.12-slim

# uv, pinned to the version that wrote uv.lock.
RUN pip install --no-cache-dir uv==0.12.24

WORKDIR /app

# UV_NO_SYNC: `uv run` uses the packages installed below and never installs anything when the container starts.
# PYTHONUNBUFFERED: each turn's log line reaches the host's logs straight away.
ENV UV_NO_SYNC=1 PYTHONUNBUFFERED=1

# The packages first, so a code change doesn't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

# Progress lives in /app/data (DATABASE_URL defaults to sqlite:///data/tutor.db): mount a volume there.
# One worker: the breakers and SQLite assume a single process. Railway and Render set PORT.
CMD uv run uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
