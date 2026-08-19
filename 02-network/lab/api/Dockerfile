# Python 3.14 per the pin table in lab/README.md.
FROM python:3.14-slim

WORKDIR /srv
RUN apt-get update && apt-get install -y --no-install-recommends \
        iproute2 curl procps dnsutils \
    && rm -rf /var/lib/apt/lists/*
# iproute2 is here for `docker compose exec api ss -tan`, which several topics
# run. `ss` does not exist on macOS, so it has to live in the container.
# dnsutils is here for `dig`, which Topic 5 runs INSIDE api on purpose: the
# answer has to come from api's own resolver, api's own /etc/resolv.conf and
# api's own ndots, and `dig` on the Mac would answer a different question.
# Without it the documented line fails with "dig: not found" and the topic's
# whole second half is unrunnable.

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py entrypoint.sh ./
EXPOSE 8000
ENTRYPOINT ["sh", "/srv/entrypoint.sh"]
