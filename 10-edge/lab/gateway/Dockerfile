# gateway - FastAPI in front of the HOST model server (topics 2 and 6).
# Python 3.14 per the lab-wide pin in SEQUENCE.md.
FROM python:3.14-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# /opt/prompt_layout is mounted from 02-continuous-batching-and-paged-kv/python
# by docker-compose.yml, so prompt rendering has exactly one implementation
# shared with the regression test. Do not copy it in here.
ENV PYTHONPATH=/opt/prompt_layout
ENV PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning"]
