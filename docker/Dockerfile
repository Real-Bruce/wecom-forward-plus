# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

# Keep logs unbuffered so `docker logs` shows output immediately.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install the project from source. README.md is required by setuptools
# (project.readme), so it must be present at build time.
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

# Run the bridge. `python -m src.main` loads .env via python-dotenv, but
# environment variables passed by compose (env_file) take precedence and are
# not overwritten by load_dotenv().
CMD ["python", "-m", "src.main"]
