FROM python:3.14-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py entrypoint.sh ./
EXPOSE 9000
ENTRYPOINT ["sh", "/srv/entrypoint.sh"]
