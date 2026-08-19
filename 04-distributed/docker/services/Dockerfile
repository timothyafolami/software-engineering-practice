FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      libfaketime curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
      "fastapi==0.115.6" "uvicorn[standard]==0.34.0" \
      "psycopg[binary,pool]==3.2.3" "httpx==0.28.1" \
      "etcd3-py==0.1.6" "confluent-kafka==2.6.1"
WORKDIR /srv
COPY app /srv/app
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
