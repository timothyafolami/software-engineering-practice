# The CLI image, so nothing has to be installed on the host.
#   docker compose run --rm tools schemathesis run http://api:8000/openapi.json
#   docker compose run --rm tools oasdiff breaking api/openapi.snapshot.json /tmp/live.json
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "schemathesis==4.24.*" "mutmut==3.7.*" "coverage==7.15.*"
# oasdiff ships as a single static binary; pinned so a breaking-change verdict
# is reproducible across sessions.
ARG OASDIFF_VERSION=1.11.7
RUN curl -sSL "https://github.com/oasdiff/oasdiff/releases/download/v${OASDIFF_VERSION}/oasdiff_${OASDIFF_VERSION}_linux_$(dpkg --print-architecture).tar.gz" \
      | tar -xz -C /usr/local/bin oasdiff || \
    echo "oasdiff download failed at build time; run it on the host instead"
WORKDIR /work
