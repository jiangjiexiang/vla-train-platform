FROM python:3.12-slim AS dependencies

WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r /app/requirements.txt

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLATFORM_DB_PATH=/data/history/platform.db \
    PLATFORM_STATIC_DIR=/app/static

WORKDIR /app
COPY --from=dependencies /install /usr/local
COPY backend/app /app/app
COPY frontend/dist /app/static

RUN mkdir -p /data/history

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
