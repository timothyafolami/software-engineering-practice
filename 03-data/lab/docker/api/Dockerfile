FROM python:3.13-slim

# Postgres client tools, so `docker compose exec api psql ...` works and so the
# container can run pg_isready against the services it depends on.
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8000

# The worker count is computed at startup from the CGROUP QUOTA, not from
# os.cpu_count() -- which inside a container reports the host's cores and lies
# to you. That is Topic 7 experiment 6, wired into the thing it is about rather
# than described next to it.
CMD ["python", "serve.py"]
