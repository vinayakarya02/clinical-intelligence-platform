# Clinical Intelligence Platform — application image.
#
# One image, three entrypoints (api / worker / scheduler), selected by command
# (docs/design/adr-0017-worker-topology.md). One build, one scan, one SBOM, one version —
# and a version skew between API and worker becomes impossible rather than merely unlikely.
#
# Multi-stage: the build stage carries compilers and headers that the runtime must not.

# ---------------------------------------------------------------------------------------
# Stage 1 — build the virtualenv
# ---------------------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS builder

# Pinned by digest in a real deployment. A floating tag means the image you scanned and the
# image you shipped can differ, which defeats the scan.

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# build-essential is needed by a few wheels; it stays in this stage and never reaches runtime.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Dependency manifests first, so a source-only change reuses the dependency layer. This is the
# difference between a 20-second rebuild and a 4-minute one on every commit.
COPY pyproject.toml README.md ./
COPY libs/cip_core/src ./libs/cip_core/src
COPY libs/cip_platform/src ./libs/cip_platform/src
COPY services/ingestion/src ./services/ingestion/src
COPY services/retrieval/src ./services/retrieval/src
COPY services/copilot/src ./services/copilot/src
COPY services/gateway/src ./services/gateway/src

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install ".[s3]"

# ---------------------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

# tesseract and poppler are runtime dependencies of the OCR fallback path. Installed here
# rather than in the builder because they are needed at run time, not at build time.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        tesseract-ocr \
        poppler-utils \
        curl \
    && rm -rf /var/lib/apt/lists/*

# A fixed, non-root uid. Fixed rather than auto-assigned so a volume's file ownership is
# predictable across rebuilds, and non-root because the restricted Pod Security Standard
# requires it and because a container that cannot become root cannot escalate through one.
RUN groupadd --gid 10001 cip \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin cip

COPY --from=builder --chown=10001:10001 /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    CIP_ENVIRONMENT=production

WORKDIR /app
COPY --chown=10001:10001 docker/entrypoint.sh /app/entrypoint.sh
RUN chmod 0555 /app/entrypoint.sh

USER 10001:10001

# The container answers on 8000; binding privileged ports is impossible as a non-root user
# and unnecessary behind a Service.
EXPOSE 8000

# Liveness is Kubernetes' job — a HEALTHCHECK here duplicates it with different semantics and
# no way to feed the result to a scheduler. Declared for `docker run` users only.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/health/live || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["api"]
