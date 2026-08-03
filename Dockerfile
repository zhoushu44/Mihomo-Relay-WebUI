FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl cron \
    && pip install --no-cache-dir flask pyyaml docker \
    && rm -rf /var/lib/apt/lists/*

COPY app.py /app/app.py

EXPOSE 7892

CMD ["bash", "-c", "service cron start && exec python app.py"]
