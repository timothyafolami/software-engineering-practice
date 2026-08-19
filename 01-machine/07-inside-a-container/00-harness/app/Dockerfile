# The harness image for every experiment in Topic 7.
#
# Deliberately NOT slim-buster or alpine: musl's allocator behaves
# differently under memory pressure, and this topic measures memory
# pressure. Use the same libc your production image uses, or the numbers
# do not transfer.
ARG PYTHON_IMAGE=python:3.13-slim
FROM ${PYTHON_IMAGE}

WORKDIR /srv

COPY app/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt

COPY app/main.py /srv/main.py
COPY local/cgroup.py /srv/cgroup.py
# 7.2's cpu_stat_watch.py imports both of these and is meant to be run from
# inside this container. It cannot be COPYed here itself -- the build context
# is 00-harness/ and it lives in a sibling topic -- so `docker compose cp` it
# into /srv, where these two are already on the path beside it.
COPY local/openloop.py /srv/openloop.py

# WORKERS is the variable experiments 7.2 and 7.4 sweep. One process per
# container is the modern default (FastAPI's own docs); this image lets you
# break that rule on purpose so you can measure what it costs.
ENV WORKERS=1
EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port 8000 --workers ${WORKERS}"]
