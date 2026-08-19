#!/bin/sh
# uvicorn's --timeout-keep-alive is the backend side of Topic 4's race. The
# default is 5 seconds, which is shorter than every load balancer default you
# will meet -- which is why `mismatched` is what you get by changing nothing.
set -e

case "${KEEPALIVE_PROFILE:-mismatched}" in
  mismatched)              KA=5  ;;
  ordered|ordered_bounded) KA=75 ;;
  *) echo "unknown KEEPALIVE_PROFILE=$KEEPALIVE_PROFILE" >&2; exit 64 ;;
esac

echo "api starting: VARIANT=${VARIANT} POOL_PROFILE=${POOL_PROFILE} TIMEOUT_PROFILE=${TIMEOUT_PROFILE} KEEPALIVE_PROFILE=${KEEPALIVE_PROFILE} (--timeout-keep-alive ${KA}) PROTO=${PROTO}"

exec uvicorn app:app \
  --host 0.0.0.0 --port 8000 \
  --workers "${UVICORN_WORKERS:-1}" \
  --timeout-keep-alive "$KA" \
  --no-access-log
