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

# pip honors the PIP_INDEX_URL env var; ARG makes it overridable via compose
# build args. Default is official PyPI — set a mirror (e.g. Tsinghua) when the
# build host has a poor route to pypi.org, or `pip install` fails with
# "Could not find a version that satisfies the requirement <pkg> (from
# versions: none)".
ARG PIP_INDEX_URL=https://pypi.org/simple

RUN pip install --no-cache-dir .

# Run the bridge. `python -m src.main` loads .env via python-dotenv, but
# environment variables passed by compose (env_file) take precedence and are
# not overwritten by load_dotenv().
CMD ["python", "-m", "src.main"]
