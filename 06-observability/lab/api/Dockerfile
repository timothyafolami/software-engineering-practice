# Layer 6 lab - the image both `api` and `worker` run from.
#
# arm64 note: python:3.13-slim publishes a linux/arm64 image, so this builds
# natively on Apple silicon. If you swap the base for something that does not,
# compose will emulate and every latency number in this layer becomes a
# measurement of qemu. `docker image inspect <image> --format
# '{{.Architecture}}'` is the check.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py worker.py ./

EXPOSE 8000

# `opentelemetry-instrument` patches fastapi, sqlalchemy, requests and logging
# before app.py is imported. That ordering is why app.py can rely on
# otelTraceID being present on every LogRecord -- and reversing it is the most
# common way a correlation-ID rollout produces empty fields.
CMD ["opentelemetry-instrument", "uvicorn", "app:app", \
     "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
