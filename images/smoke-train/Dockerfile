FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir torch==2.5.1+cu124 \
        --index-url https://download.pytorch.org/whl/cu124

WORKDIR /app
COPY train.py /app/train.py

ENV PYTHONUNBUFFERED=1

CMD ["python", "/app/train.py"]
