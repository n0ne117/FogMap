# syntax=docker/dockerfile:1
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build context is the repository root, so that VERSION - the single source of
# truth - can be copied into the image. Dependency installs come before any
# source copy, so editing code never reinstalls numpy.

FROM python:3.12-slim AS deps
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app
WORKDIR /app
COPY api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Test dependencies branch off the runtime dependency layer and are never
# present in the shipped image.
FROM deps AS testdeps
COPY api/requirements-dev.txt ./requirements-dev.txt
RUN pip install --no-cache-dir -r requirements-dev.txt

FROM deps AS runtime
COPY api/pyproject.toml ./pyproject.toml
COPY api/fogmap ./fogmap
COPY VERSION ./VERSION
EXPOSE 8000
CMD ["uvicorn", "fogmap.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM testdeps AS test
COPY api/pyproject.toml ./pyproject.toml
COPY api/fogmap ./fogmap
COPY api/tests ./tests
COPY VERSION ./VERSION
CMD ["pytest"]
