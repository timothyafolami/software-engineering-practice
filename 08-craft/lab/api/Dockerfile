FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir \
      "fastapi>=0.115" "uvicorn[standard]>=0.34" "sqlalchemy>=2.0.39" \
      "asyncpg>=0.30" "pydantic>=2.10" "httpx>=0.28" \
      "pytest==9.1.*" pytest-asyncio pytest-repeat \
      "hypothesis==6.165.*" "coverage==7.15.*" "mutmut==3.7.*" \
      "testcontainers==4.15.*" aiosqlite
COPY . /app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
