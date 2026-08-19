#!/bin/sh
# Topic 6 needs the upstream to speak HTTP/2. uvicorn does not; hypercorn does,
# over cleartext with prior knowledge (h2c), which is what httpx negotiates
# when you give it http:// and http2=True.
set -e
if [ "${PROTO:-h1}" = "h2" ]; then
  echo "upstream ${NAME} starting on hypercorn, h2c (PROTO=h2)"
  exec hypercorn app:app --bind 0.0.0.0:9000 --keep-alive 75
fi
echo "upstream ${NAME} starting on uvicorn, HTTP/1.1 (PROTO=${PROTO:-h1})"
exec uvicorn app:app --host 0.0.0.0 --port 9000 --timeout-keep-alive 75 --no-access-log
