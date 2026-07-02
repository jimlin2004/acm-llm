FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ngspice \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app

ENV PORT=9000 \
    LOG_LEVEL=INFO \
    NGSPICE_BIN=ngspice

EXPOSE 9000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 4"]
