#!/usr/bin/env bash
set -e

echo "[$(date)] Ждём доступности Postgres..."
# Подождём БД (простая проверка)
until python - << 'EOF'
import os
import psycopg2

host = os.getenv("DB_HOST", "db")
port = int(os.getenv("DB_PORT", "5432"))
name = os.getenv("DB_NAME")
user = os.getenv("DB_USER")
password = os.getenv("DB_PASSWORD")

try:
    conn = psycopg2.connect(
        host=host,
        port=port,
        dbname=name,
        user=user,
        password=password,
    )
    conn.close()
except Exception as e:
    raise SystemExit(1)
EOF
do
  echo "Postgres ещё не готов, спим 2 секунды..."
  sleep 2
done

echo "[$(date)] Postgres доступен, запускаем инициализацию из Excel..."
python -m paunds.init_from_xlsx

echo "[$(date)] Инициализация завершена (или пропущена)."

if [ "$#" -gt 0 ]; then
  echo "[$(date)] Запускаем переданную команду: $*"
  exec "$@"
fi

echo "[$(date)] Запускаем основной воркер..."
exec python -m paunds.main
