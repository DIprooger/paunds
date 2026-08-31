FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# системные зависимости (для psycopg2 и т.п.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Python-зависимости
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Копируем ВЕСЬ проект в /app/paunds
COPY . /app/paunds

# entrypoint лежит внутри /app/paunds/entrypoint.sh
RUN chmod +x /app/paunds/entrypoint.sh

# Рабочая директория остаётся /app, здесь Python будет видеть пакет "paunds"
WORKDIR /app

ENTRYPOINT ["/app/paunds/entrypoint.sh"]

