#!/bin/sh
# Selects which of the three roles this container plays (ADR-0017).
#
# `exec` matters: without it the shell stays PID 1 and does not forward SIGTERM, so
# Kubernetes' graceful shutdown becomes a 30-second wait followed by SIGKILL — which for the
# worker means a job killed mid-write on every single rollout.
set -eu

ROLE="${1:-api}"
shift 2>/dev/null || true

case "$ROLE" in
  api)
    exec python -m uvicorn cip_gateway.app:create_app \
      --factory \
      --host 0.0.0.0 \
      --port "${CIP_PORT:-8000}" \
      --workers "${CIP_WEB_CONCURRENCY:-2}" \
      --no-server-header \
      --timeout-graceful-shutdown "${CIP_GRACEFUL_TIMEOUT:-25}"
    ;;
  worker)
    exec python -m cip_gateway.worker "$@"
    ;;
  scheduler)
    exec python -m cip_gateway.scheduler "$@"
    ;;
  migrate)
    exec python -m cip_ingestion.cli db upgrade
    ;;
  *)
    echo "Unknown role '$ROLE'. Expected: api | worker | scheduler | migrate" >&2
    exit 64
    ;;
esac
