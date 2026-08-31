# Paunds (парсер Investing.com → Google Sheets + сигналы Plus500)

## Назначение

Сервис периодически (по сетке **HH:00 / HH:30**) получает HTML страниц инструментов (через Firecrawl), парсит:
- цену,
- сигналы по таймфреймам (30m/1h/5h/1d/1w/1mo),
- блок *Technical Indicators*,
- блок *Pivot Points*,

затем:
- пишет строку в соответствующий лист Google Sheets,
- для **GBPUSD** ведёт состояние позиции в Postgres и, при определённых условиях, отправляет сигнал во внешний backend Plus500,
- уведомляет в Telegram (обычные чаты и отдельный error-чат).

## Архитектура

- `main.py` — режим запуска: единичный (`run_once`) или периодический (`run_forever`) в зависимости от `INTERVAL_SECONDS`.
- `worker.py` — основная логика: планирование слотов, параллельный парсинг инструментов, запись в Sheets/DB, логика сигналов GBPUSD.
- `firecrawl_client.py` — получение HTML через Firecrawl (анти-кеш параметр, обработка ошибок/антибот страниц).
- `parsers.py` — парсинг таймфреймов + technical indicators + pivot points из HTML.
- `sheets_client.py` — запись строк в Google Sheets (+ ретраи) и чтение строки листа «Бот».
- `telegram_client.py` — отправка уведомлений и ошибок в Telegram (+ ретраи).
- `plus500_client.py` — отправка `/signal`, `/prepare`, `/close_page` на внешний Plus500 backend.
- `db/init.sql` — таблицы Postgres для хранения истории GBPUSD и состояния позиции.

## Требования

- Python 3.12+
- Postgres (если используете режим с сохранением GBPUSD/position_state)
- Доступ к:
  - Firecrawl API
  - Google Sheets API (service account)
  - Telegram Bot API
  - вашему Plus500 backend (эндпоинты `/signal`, `/prepare`, `/close_page`)

Зависимости фиксируются в `requirements.txt`.

## Переменные окружения

### Обязательные для полноценной работы

- `FIRECRAWL_API_KEY` — ключ Firecrawl.
- `GOOGLE_SHEET_ID` — ID Google Spreadsheet.
- `GOOGLE_SERVICE_ACCOUNT_FILE` — путь к `credentials.json` (по умолчанию `/app/credentials.json`).

### Telegram

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_IDS` — список chat_id через запятую (например: `123,456`).
- `TELEGRAM_ERROR_CHAT_ID` — отдельный chat_id для ошибок.

### Plus500 backend

- `PLUS500_SIGNAL_URL` — базовый URL `/signal` (по умолчанию `https://plus500.miraginvest.com/signal`).
- `PLUS500_SIGNAL_SECRET` — секрет, который будет отправляться в payload.

Опционально можно переопределить конкретные URL:
- `PLUS500_PREPARE_URL` (по умолчанию `{BASE}/prepare`)
- `PLUS500_CLOSE_PAGE_URL` (по умолчанию `{BASE}/close_page`)

### Postgres

Используются переменные (типично в docker-compose):
- `DB_HOST` (default: `db`)
- `DB_PORT` (default: `5432`)
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`

### Планировщик и ретраи

- `INTERVAL_SECONDS` — если `> 0`, включается периодический режим; если `<= 0` — единичный запуск.
- `TIMEZONE` — таймзона для слотов (по умолчанию `Europe/Kaliningrad`).
- `PREPARE_OFFSET_MIN` — за сколько минут до слота отправлять `/prepare` (по умолчанию `1.2`).
- `MAX_RETRIES` (по умолчанию `3`)
- `RETRY_DELAY_SECONDS` (по умолчанию `3`)

### Инструменты и листы

Инструменты задаются в `INSTRUMENTS` (через env можно переопределять имя/URL/лист). Минимально важные переменные листов:
- `SHEET_NAME_GBPUSD` (default: `DATA FOR GBPUSD`)
- `SHEET_NAME_BITCOIN` (default: `DATA FOR BITCOIN`)
- `SHEET_NAME_AUDUSD` (default: `DATA FOR AUDUSD`)
- `SHEET_NAME_EURGBP` (default: `DATA FOR EURGBP`)
- `SHEET_NAME_USDJPY` (default: `DATA FOR USDJPY`)
- `SHEET_NAME_USDCAD` (default: `DATA FOR USDCAD`)
- `SHEET_NAME_EURUSD` (default: `DATA FOR EURUSD`)
- `SHEET_NAME_SPX` (default: `DATA FOR S&P`)
- `SHEET_NAME_BOT` (default: `Бот`)

## Формат записи в Google Sheets

В каждый лист инструмента добавляется строка следующей структуры:

`TIME, PRICE, 30 MIN, HOURLY, 5 HOURS, DAILY, WEEKLY, MONTHLY`

Значения таймфреймов — нормализованные сигналы: `STRONG BUY / STRONG SELL / BUY / SELL / NEUTRAL`.

## Логика работы

### Режимы запуска

- **Единичный запуск**: `INTERVAL_SECONDS <= 0`
- **Периодический режим**: `INTERVAL_SECONDS > 0`
  - следующий запуск выравнивается по ближайшему слоту `HH:00` или `HH:30`,
  - перед слотом выполняется `/prepare` (если `PREPARE_OFFSET_MIN > 0`),
  - затем выполняется `run_once()`.

### Будни/выходные

В `run_once()`:
- в будни обрабатываются все инструменты,
- в выходные — только BITCOIN.

## GBPUSD: DB + Plus500 (инициализация и торговая логика)

Для инструмента **GBPUSD** используется расширенная логика с базой данных и отправкой сигналов во внешний backend Plus500.

### Что происходит для GBPUSD

- **Всегда** пишется история в таблицу `data_for_gbpusd`:
  - время слота,
  - цена,
  - сигналы по таймфреймам (30m / 1h / 5h / 1d / 1w / 1mo).

- В таблицу `bot` записываются **только сильные сигналы**:
  - `STRONG BUY`
  - `STRONG SELL`.

- Состояние текущей позиции хранится в таблице `position_state`:
  - `NONE`
  - `LONG`
  - `SHORT`.

- При выполнении условий:
  - формируется торговое действие,
  - отправляется запрос в Plus500 backend (`/signal`),
  - параллельно отправляется уведомление в Telegram.

### Обязательный импорт данных при первом запуске

⚠️ **Перед первым запуском сервиса необходимо выполнить импорт исторических данных для GBPUSD.**

Импорт нужен для:
- заполнения `data_for_gbpusd`,
- инициализации таблицы `bot`,
- корректной работы логики сигналов и состояния позиции.

#### Источник данных

- Excel-файл:
  ```
  Статистика форекс.xlsx
  ```
- Лист:
  ```
  DATA FOR GBPUSD
  ```

Формат строк:
```
TIME | PRICE | 30m | 1h | 5h | 1d | 1w | 1mo
```

Все таймфреймы обязательны — строки с пустыми значениями пропускаются.

#### Что делает импорт

При запуске импорта:

1. Полностью очищаются таблицы:
   - `data_for_gbpusd`
   - `bot`

2. Каждая валидная строка:
   - всегда добавляется в `data_for_gbpusd`,
   - добавляется в `bot **только** если сигнал 30m равен
     `STRONG BUY` или `STRONG SELL`.

#### Как запустить импорт

Импорт выполняется автоматически при старте Docker-контейнера.

Для ручного запуска:
```bash
python -m paunds.init_from_xlsx
```

Можно переопределить путь к файлу:
```bash
FOREX_XLSX_PATH="/path/to/Статистика форекс.xlsx"
```

⚠️ Импорт предназначен **только для первичной инициализации**.  
Повторный запуск полностью перезапишет данные в БД.


## Быстрый старт (локально)

1) Создайте виртуальное окружение и установите зависимости:
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

2) Создайте `.env` (или экспортируйте переменные окружения) и положите `credentials.json`.

3) Запуск:
```bash
python -m paunds.main
```

## Запуск в Docker

В репозитории есть `Dockerfile`, `docker-compose.yml` и `entrypoint.sh`.

Логика entrypoint:
1) ждёт доступности Postgres,
2) запускает инициализацию из Excel (`python -m paunds.init_from_xlsx`),
3) запускает сервис (`python -m paunds.main`).

Типовой запуск:
```bash
docker compose up --build
```

## База данных

DDL находится в `db/init.sql`. Таблицы:
- `data_for_gbpusd` — история по слотам (цена + таймфреймы),
- `bot` — вспомогательная таблица для логики сигналов,
- `position_state` — текущее состояние позиции (`NONE/LONG/SHORT`).

## Логи

- уровень: `LOG_LEVEL` (default: `INFO`)
- файл: `LOG_FILE` (default: `pounds.log`)

## Тесты

В проекте есть тесты логики (`test_decision_logic.py`, `test_plus500.py`). Запуск (пример):
```bash
python -m pytest -q
```


# paunds — parser-service / стратегия / API

## Назначение проекта

`paunds` — основной parser-service. Он отвечает за:

* получение и обработку сигналов;
* хранение стратегии в PostgreSQL;
* хранение списка инструментов;
* хранение правил по дням недели и таймфреймам;
* хранение количества покупки по каждой паре;
* API для веб-панели `plus500-web`;
* отправку команд в Plus500 worker (`rb_worker`): `/prepare`, `/open`, `/close`.

## Основные компоненты

```text
db_client.py          # работа с PostgreSQL, стратегия, rules, instruments, trade_amount
api_main.py           # FastAPI приложение
api_auth.py           # авторизация API через X-API-Key
api_strategies.py     # endpoints стратегий
phase1_logic.py       # логика открытия/закрытия по сигналам
plus500_client.py     # HTTP-клиент для rb_worker
worker.py             # основной worker/parser loop
docker-compose.yml    # PostgreSQL + сервисы
.env                  # локальные настройки и секреты
```

## База данных

PostgreSQL запускается в Docker. В локальной разработке используется подключение:

```bash
DB_HOST=127.0.0.1
DB_PORT=55432
```

Проверка подключения и схемы:

```bash
cd /home/diana/PycharmProjects/paunds
source venv/bin/activate

DB_HOST=127.0.0.1 DB_PORT=55432 python - <<'PY'
from dotenv import load_dotenv
load_dotenv(dotenv_path=".env")

from db_client import ensure_schema, list_strategy_instruments, list_instrument_trade_settings

ensure_schema()

print("=== instruments ===")
for x in list_strategy_instruments():
    print(x)

print("=== trade settings ===")
for x in list_instrument_trade_settings():
    print(x)
PY
```

## Запуск parser API

Parser API нужен для связи с `plus500-web`.

```bash
cd /home/diana/PycharmProjects/paunds
source venv/bin/activate

DB_HOST=127.0.0.1 DB_PORT=55432 python -m uvicorn api_main:app --host 0.0.0.0 --port 8080
```

Проверка:

```bash
curl -s http://127.0.0.1:8080/health | python -m json.tool
```

Ожидаемый ответ:

```json
{
  "ok": true,
  "service": "paunds-parser-api"
}
```

Если порт занят:

```bash
ss -ltnp | grep ':8080' || true
```

Перезапуск:

```bash
pkill -f "uvicorn api_main:app" || true

cd /home/diana/PycharmProjects/paunds
source venv/bin/activate

DB_HOST=127.0.0.1 DB_PORT=55432 python -m uvicorn api_main:app --host 0.0.0.0 --port 8080
```

## API авторизация

API защищён через заголовок:

```text
X-API-Key: <PARSER_API_KEY>
```

Ключ лежит в `.env`:

```bash
grep '^PARSER_API_KEY=' .env
```

## Основные API endpoints

```text
GET    /health
GET    /api/v1/strategies/options
GET    /api/v1/strategies/instruments
GET    /api/v1/strategies/instrument?strategy_key=GBP/USD
PUT    /api/v1/strategies/instrument/week
POST   /api/v1/strategies/instrument
PATCH  /api/v1/strategies/instrument/signals-enabled
PATCH  /api/v1/strategies/instrument/trade-amount
DELETE /api/v1/strategies/instrument?strategy_key=GBP/USD
GET    /api/v1/strategies/export
```

## Проверить список пар

```bash
cd /home/diana/PycharmProjects/paunds

API_KEY="$(grep '^PARSER_API_KEY=' .env | cut -d= -f2-)"

curl -s \
  -H "X-API-Key: $API_KEY" \
  http://127.0.0.1:8080/api/v1/strategies/instruments \
  | python -m json.tool
```

## Проверить конкретную пару

```bash
cd /home/diana/PycharmProjects/paunds

API_KEY="$(grep '^PARSER_API_KEY=' .env | cut -d= -f2-)"

curl -sG \
  -H "X-API-Key: $API_KEY" \
  --data-urlencode "strategy_key=GBP/USD" \
  http://127.0.0.1:8080/api/v1/strategies/instrument \
  | python -m json.tool
```

API должен понимать оба варианта:

```text
GBP/USD
GBPUSD
```

## Задать количество покупки

```bash
cd /home/diana/PycharmProjects/paunds

API_KEY="$(grep '^PARSER_API_KEY=' .env | cut -d= -f2-)"

curl -s \
  -X PATCH \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8080/api/v1/strategies/instrument/trade-amount \
  -d '{
    "strategy_key": "GBP/USD",
    "trade_amount": 1000
  }' | python -m json.tool
```

Проверка:

```bash
curl -sG \
  -H "X-API-Key: $API_KEY" \
  --data-urlencode "strategy_key=GBP/USD" \
  http://127.0.0.1:8080/api/v1/strategies/instrument \
  | python -m json.tool \
  | grep -E 'trade_amount|trade_amount_currency|trade_amount_unit'
```

Ожидаемый пример:

```text
"trade_amount": 1000.0
"trade_amount_raw": "1000.00000000"
"trade_amount_currency": "GBP"
"trade_amount_unit": "BASE_CURRENCY"
```

## Восстановить / создать активную пару с правилами

```bash
cd /home/diana/PycharmProjects/paunds

API_KEY="$(grep '^PARSER_API_KEY=' .env | cut -d= -f2-)"

curl -s \
  -X POST \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8080/api/v1/strategies/instrument \
  -d '{
    "strategy_key": "GBP/USD",
    "signals_enabled": true,
    "trade_amount": 1000,
    "rules": [
      {
        "day_of_week": 0,
        "start": "00:00",
        "end": "24:00",
        "timeframe": "30m",
        "signal": "BUY_ON_STRONG_BUY"
      },
      {
        "day_of_week": 0,
        "start": "00:00",
        "end": "24:00",
        "timeframe": "1h",
        "signal": "SELL_ON_STRONG_SELL"
      }
    ]
  }' | python -m json.tool
```

## Проверить активные rules

```bash
cd /home/diana/PycharmProjects/paunds
source venv/bin/activate

DB_HOST=127.0.0.1 DB_PORT=55432 python scripts/sync_strategy_stage1_data.py
```

Важно: обычный запуск `sync_strategy_stage1_data.py` безопасный. Он синхронизирует инструменты и не перезаписывает правила.

Не запускать destructive replace-режим без необходимости.

## Связь с rb_worker

`plus500_client.py` отправляет команды в `rb_worker`.

Открытие позиции идёт через:

```text
PLUS500_OPEN_URL
```

Подготовка:

```text
PLUS500_PREPARE_URL
```

Закрытие позиции:

```text
PLUS500_CLOSE_POSITION_URL
```

Сейчас payload `/open` содержит:

```json
{
  "secret": "...",
  "pair": "GBPUSD",
  "action": "BUY",
  "trade_amount": "1000",
  "amount": "1000",
  "trade_amount_currency": "GBP",
  "trade_amount_unit": "BASE_CURRENCY"
}
```

## Безопасность открытия

В `.env` есть два важных флага:

```env
PLUS500_ACTIONS_ENABLED=1
PLUS500_DRY_RUN=1
```

Режимы:

```text
PLUS500_ACTIONS_ENABLED=0  -> вообще не отправлять команды в rb_worker
PLUS500_ACTIONS_ENABLED=1  -> разрешить отправку команд
PLUS500_DRY_RUN=1          -> логировать, но не открывать реально
PLUS500_DRY_RUN=0          -> реальное действие
```

Для тестов сначала использовать:

```env
PLUS500_ACTIONS_ENABLED=1
PLUS500_DRY_RUN=1
```

## Git / что не коммитить

Не коммитить:

```text
.env
.env.bak
venv/
__pycache__/
backups/
pgdata/
psql
*.pyc
```

Перед коммитом:

```bash
cd /home/diana/PycharmProjects/paunds
git status --short
```

Проверить синтаксис:

```bash
source venv/bin/activate
python -m py_compile db_client.py api_main.py api_auth.py api_strategies.py plus500_client.py phase1_logic.py worker.py
```
