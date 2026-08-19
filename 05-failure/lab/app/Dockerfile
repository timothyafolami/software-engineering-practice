# Layer 5 lab - one image, every role. ROLE picks the behaviour at runtime.
#
# python:3.13-slim rather than alpine: this lab measures latency, and musl's
# allocator and DNS resolver are a variable nobody asked for here.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt /srv/app/requirements.txt
RUN pip install --no-cache-dir -r /srv/app/requirements.txt

COPY . /srv/app/

# tools/ is mounted at /srv/tools by compose, so `python -m tools.zombie_report`
# works inside any of the four application containers.
ENV PYTHONPATH=/srv

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --retries=20 --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"

CMD ["python", "-m", "app.run"]
