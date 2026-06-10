# syntax=docker/dockerfile:1
#
# Multi-stage image for the FPT Cloud health-check framework.
# Targets: base -> cli | producer | worker | reaper
#
# Build a specific role:   docker build --target worker -t hc-worker .
# Build the CLI:           docker build --target cli    -t hc-cli    .

# ──────────────────────────────────────────────────────────────────────────
# base — Python 3.11 runtime + pinned Terraform CLI + the hc package.
# ──────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

# C-003: Terraform CLI must be on PATH in the worker. Pin version + SHA256.
# Checksums from https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/
ARG TERRAFORM_VERSION=1.9.8
ARG TERRAFORM_SHA256=186e0145f5e5f2eb97cbd785bc78f21bae4ef15119349f6ad4fa535b83b10df8

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TF_PLUGIN_CACHE_DIR=/opt/tf-plugin-cache

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl unzip; \
    curl -fsSL -o /tmp/terraform.zip \
        "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip"; \
    echo "${TERRAFORM_SHA256}  /tmp/terraform.zip" | sha256sum -c -; \
    unzip /tmp/terraform.zip -d /usr/local/bin; \
    rm /tmp/terraform.zip; \
    terraform version; \
    apt-get purge -y --auto-remove curl unzip; \
    rm -rf /var/lib/apt/lists/*; \
    mkdir -p "${TF_PLUGIN_CACHE_DIR}"

WORKDIR /app

# Install the package. Copy metadata first so the dependency layer is cached
# independently of source changes.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Terraform modules are part of the runtime image (rendered per task).
COPY modules ./modules

# Default to a non-root user for the runtime stages.
RUN useradd --create-home --uid 10001 hc && \
    chown -R hc:hc /app "${TF_PLUGIN_CACHE_DIR}"
USER hc

# ──────────────────────────────────────────────────────────────────────────
# cli — operator entrypoint (queue/dlq/report inspection).
# ──────────────────────────────────────────────────────────────────────────
FROM base AS cli
ENTRYPOINT ["hc"]
CMD ["--help"]

# ──────────────────────────────────────────────────────────────────────────
# producer — enqueues a checklist run. (entrypoint lands in Phase 3)
# ──────────────────────────────────────────────────────────────────────────
FROM base AS producer
ENTRYPOINT ["python", "-m", "hc.producer"]

# ──────────────────────────────────────────────────────────────────────────
# worker — pulls tasks, runs Terraform, validates. (entrypoint lands in Phase 5)
# ──────────────────────────────────────────────────────────────────────────
FROM base AS worker
ENTRYPOINT ["python", "-m", "hc.worker"]

# ──────────────────────────────────────────────────────────────────────────
# reaper — singleton that reclaims idle PEL entries and drives the scheduler.
# ──────────────────────────────────────────────────────────────────────────
FROM base AS reaper
ENTRYPOINT ["python", "-m", "hc.reaper"]
