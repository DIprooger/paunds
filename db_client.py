# db_client.py
import os
import json
import logging
from typing import Optional, Dict, Any
from datetime import datetime
from typing import Union
from decimal import Decimal, InvalidOperation
import psycopg2
from psycopg2 import errors

logger = logging.getLogger(__name__)

DB_HOST = os.getenv("DB_HOST", "db")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

# Управление схемой/сигналами через env
DB_ENABLE_SIGNAL_TRIGGER = os.getenv("DB_ENABLE_SIGNAL_TRIGGER", "1").strip() == "1"

# ----- Low-level helpers

def _get_conn():
    if not (DB_NAME and DB_USER and DB_PASSWORD):
        logger.error("DB env is missing: DB_NAME/DB_USER/DB_PASSWORD")
        return None
    try:
        return psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
        )
    except Exception:
        logger.exception("Не удалось подключиться к Postgres")
        return None


def _parse_dt(executed_at: Union[str, datetime]) -> datetime:
    # 1) если уже datetime — возвращаем как есть (делаем naive, т.к. в БД TIMESTAMP без TZ)
    if isinstance(executed_at, datetime):
        return executed_at.replace(tzinfo=None)

    s = str(executed_at or "").strip()
    if not s:
        raise ValueError("executed_at is empty")

    # 2) ISO / ISO+TZ
    try:
        # поддержка "Z" если вдруг появится
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except Exception:
        pass

    # 3) твой формат из Google Sheets: "09.01.2026 12.15"
    for fmt in (
        "%d.%m.%Y %H.%M",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y %H.%M.%S",
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue

    raise ValueError(f"Unsupported executed_at format: {s!r}")

# ----- Schema management

def ensure_schema() -> None:
    """
    Идемпотентно создаёт/обновляет схему БД до актуальной версии:
    - базовые таблицы
    - instrument_signals.timeframe
    - уникальность (instrument_code, timeframe, candle_time)
    - триггер, который выводит STRONG BUY/SELL из instrument_data в instrument_signals (по всем TF)
    """
    conn = _get_conn()
    if not conn:
        raise RuntimeError("ensure_schema: нет соединения с БД")

    try:
        with conn.cursor() as cur:
            # 1) Base tables
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS app_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT '',
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS instruments (
                    code TEXT PRIMARY KEY,
                    sheet_name TEXT UNIQUE NOT NULL,
                    plus500_name TEXT,
                    signals_enabled BOOLEAN NOT NULL DEFAULT FALSE
                );

                CREATE TABLE IF NOT EXISTS instrument_data (
                    id BIGSERIAL PRIMARY KEY,
                    instrument_code TEXT NOT NULL REFERENCES instruments(code) ON DELETE CASCADE,
                    candle_time TIMESTAMP NOT NULL,
                    price NUMERIC(18,8),
                    min_30 TEXT,
                    hourly TEXT,
                    hours_5 TEXT,
                    daily TEXT,
                    weekly TEXT,
                    monthly TEXT,
                    UNIQUE(instrument_code, candle_time)
                );

                CREATE INDEX IF NOT EXISTS ix_instrument_data_code_time
                ON instrument_data(instrument_code, candle_time);

                CREATE TABLE IF NOT EXISTS instrument_signals (
                    id BIGSERIAL PRIMARY KEY,
                    instrument_code TEXT NOT NULL REFERENCES instruments(code) ON DELETE CASCADE,
                    candle_time TIMESTAMP NOT NULL,
                    price NUMERIC(18,8),
                    min_30 TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS position_state (
                    id BIGSERIAL PRIMARY KEY,
                    instrument_code TEXT NOT NULL REFERENCES instruments(code) ON DELETE CASCADE,
                    state TEXT NOT NULL,
                    comment TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS ix_position_state_code_id
                ON position_state(instrument_code, id);
                """
            )

            # 2) Migrations: timeframe + index
            cur.execute(
                """
                ALTER TABLE instrument_signals
                ADD COLUMN IF NOT EXISTS timeframe TEXT NOT NULL DEFAULT '30m';
                """
            )

            cur.execute(
                """
                ALTER TABLE position_state
                  ADD COLUMN IF NOT EXISTS position_num BIGINT;

                ALTER TABLE position_state
                  ADD COLUMN IF NOT EXISTS opened_at TIMESTAMP;

                ALTER TABLE position_state
                  ADD COLUMN IF NOT EXISTS position_tf TEXT;
                """
            )

            # Уникальность обязательна, чтобы безопасно делать backfill и исключить дубли
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_instrument_signals_code_tf_time
                ON instrument_signals(instrument_code, timeframe, candle_time);
                """
            )

            # Индекс (instrument_code, candle_time) больше не обязателен, но если он уже есть — не трогаем.
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_instrument_signals_code_time
                ON instrument_signals(instrument_code, candle_time);
                """
            )

            cur.execute(
                """
                ALTER TABLE position_state
                    ADD COLUMN IF NOT EXISTS entry_rule TEXT;
                """
            )

            cur.execute(
                """
                ALTER TABLE position_state
                  ADD COLUMN IF NOT EXISTS closed_at TIMESTAMP;

                ALTER TABLE position_state
                  ADD COLUMN IF NOT EXISTS owner_tf TEXT;
                """
            )


            # 2.5) Strategy schedule schema for web/API control
            cur.execute(
                """
                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS strategy_key TEXT;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS display_name TEXT;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;

                CREATE UNIQUE INDEX IF NOT EXISTS ux_instruments_strategy_key
                ON instruments(strategy_key)
                WHERE strategy_key IS NOT NULL;

                CREATE TABLE IF NOT EXISTS strategy_settings (
                    key TEXT PRIMARY KEY,
                    value_json JSONB NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                );

                INSERT INTO strategy_settings(key, value_json)
                VALUES(
                    'tf_priority',
                    '["30m", "1h", "5h", "1d", "1w", "1mo"]'::jsonb
                )
                ON CONFLICT (key) DO NOTHING;

                CREATE TABLE IF NOT EXISTS strategy_rules (
                    id BIGSERIAL PRIMARY KEY,

                    instrument_code TEXT NOT NULL REFERENCES instruments(code) ON DELETE CASCADE,

                    day_of_week SMALLINT NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
                    row_order INTEGER NOT NULL DEFAULT 0,

                    start_time TEXT NOT NULL DEFAULT '00:00',
                    end_time TEXT NOT NULL DEFAULT '24:00',

                    timeframe TEXT NOT NULL,
                    signal TEXT NOT NULL,

                    is_active BOOLEAN NOT NULL DEFAULT TRUE,

                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),

                    CHECK (timeframe IN ('30m', '1h', '5h', '1d', '1w', '1mo')),
                    CHECK (signal IN (
                        'BUY_ON_STRONG_BUY',
                        'BUY_ON_STRONG_SELL',
                        'SELL_ON_STRONG_BUY',
                        'SELL_ON_STRONG_SELL'
                    ))
                );
                
                CREATE TABLE IF NOT EXISTS strategy_cycle_state (
                    instrument_code TEXT PRIMARY KEY
                        REFERENCES instruments(code) ON DELETE CASCADE,
                
                    current_skip INTEGER NOT NULL DEFAULT 0,
                    waiting_transition BOOLEAN NOT NULL DEFAULT FALSE,
                
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS ix_strategy_rules_instrument_day
                ON strategy_rules(instrument_code, day_of_week, row_order, id)
                WHERE is_active = TRUE;

                UPDATE instruments
                SET strategy_key = CASE code
                    WHEN 'GBPUSD' THEN 'GBP/USD'
                    WHEN 'EURGBP' THEN 'EUR/GBP'
                    WHEN 'AUDUSD' THEN 'AUD/USD'
                    WHEN 'EURUSD' THEN 'EUR/USD'
                    WHEN 'USDJPY' THEN 'USD/JPY'
                    WHEN 'USDCAD' THEN 'USD/CAD'
                    WHEN 'S&P500' THEN 'S&P 500'
                    WHEN 'BITCOIN' THEN 'BITCOIN'
                    WHEN 'SOLBTC' THEN 'SOL/BTC'
                    WHEN 'EURCHF' THEN 'EUR/CHF'
                    WHEN 'AUDNZD' THEN 'AUD/NZD'
                    WHEN 'CHFJPY' THEN 'CHF/JPY'
                    ELSE COALESCE(NULLIF(plus500_name, ''), code)
                END
                WHERE strategy_key IS NULL OR strategy_key = '';

                UPDATE instruments
                SET display_name = COALESCE(NULLIF(display_name, ''), strategy_key, plus500_name, code)
                WHERE display_name IS NULL OR display_name = '';
                """
            )


            # 2.6) Plus500 instrument metadata schema
            cur.execute(
                """
                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS instrument_kind TEXT;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS plus500_symbol TEXT;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS strategy_base_currency TEXT;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS strategy_quote_currency TEXT;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS plus500_base_currency TEXT;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS plus500_quote_currency TEXT;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS amount_unit TEXT;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS amount_currency TEXT;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS price_currency TEXT;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS margin_currency TEXT;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS plus500_trade_enabled BOOLEAN NOT NULL DEFAULT FALSE;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS plus500_min_unit_amount NUMERIC(18,8);

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS plus500_initial_margin_pct NUMERIC(10,4);

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS plus500_maintenance_margin_pct NUMERIC(10,4);

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS plus500_leverage TEXT;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS plus500_details_url TEXT;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS plus500_details_updated_at TIMESTAMP;

                CREATE INDEX IF NOT EXISTS ix_instruments_plus500_symbol
                ON instruments(plus500_symbol);

                DO $$
                BEGIN
                  IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_instruments_instrument_kind'
                  ) THEN
                    ALTER TABLE instruments
                    ADD CONSTRAINT ck_instruments_instrument_kind
                    CHECK (
                      instrument_kind IS NULL
                      OR instrument_kind IN ('FOREX', 'INDEX', 'CRYPTO')
                    );
                  END IF;

                  IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_instruments_amount_unit'
                  ) THEN
                    ALTER TABLE instruments
                    ADD CONSTRAINT ck_instruments_amount_unit
                    CHECK (
                      amount_unit IS NULL
                      OR amount_unit IN ('BASE_CURRENCY', 'QUOTE_CURRENCY', 'CONTRACT')
                    );
                  END IF;
                END $$;
                """
            )


            # 2.7) Strategy API audit/change log
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_rule_changes (
                    id BIGSERIAL PRIMARY KEY,
                    instrument_code TEXT REFERENCES instruments(code) ON DELETE SET NULL,
                    strategy_key TEXT,
                    change_type TEXT NOT NULL,
                    old_rules_json JSONB,
                    new_rules_json JSONB,
                    actor TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS ix_strategy_rule_changes_instrument_created
                ON strategy_rule_changes(instrument_code, created_at DESC);
                """
            )


            # 2.8) Trade amount / Plus500 amount restrictions
            cur.execute(
                """
                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS trade_amount NUMERIC(18,8);

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS trade_amount_currency TEXT;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS trade_amount_unit TEXT;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS trade_amount_updated_at TIMESTAMP;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS plus500_amount_step NUMERIC(18,8);

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS plus500_max_unit_amount NUMERIC(18,8);

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS plus500_restrictions_note TEXT;

                ALTER TABLE instruments
                  ADD COLUMN IF NOT EXISTS plus500_restrictions_updated_at TIMESTAMP;

                DO $$
                BEGIN
                  IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_instruments_trade_amount_positive'
                  ) THEN
                    ALTER TABLE instruments
                    ADD CONSTRAINT ck_instruments_trade_amount_positive
                    CHECK (trade_amount IS NULL OR trade_amount > 0);
                  END IF;

                  IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_instruments_plus500_amount_step_positive'
                  ) THEN
                    ALTER TABLE instruments
                    ADD CONSTRAINT ck_instruments_plus500_amount_step_positive
                    CHECK (plus500_amount_step IS NULL OR plus500_amount_step > 0);
                  END IF;

                  IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_instruments_plus500_max_unit_amount_positive'
                  ) THEN
                    ALTER TABLE instruments
                    ADD CONSTRAINT ck_instruments_plus500_max_unit_amount_positive
                    CHECK (plus500_max_unit_amount IS NULL OR plus500_max_unit_amount > 0);
                  END IF;
                END $$;
                """
            )

            # 3) Trigger: поддержка instrument_signals из instrument_data
            if DB_ENABLE_SIGNAL_TRIGGER:
                _ensure_signal_trigger(cur)
                drop_trade_schema_v2_tables(cur)

        conn.commit()
    finally:
        conn.close()


def _ensure_signal_trigger(cur) -> None:
    """
    Создаёт/обновляет триггер:
    - при INSERT в instrument_data автоматически вставляет STRONG BUY/SELL в instrument_signals
      по каждому таймфрейму, который присутствует в строке (30m/1h/5h/1d/1w/1mo).
    """
    cur.execute(
        """
        CREATE OR REPLACE FUNCTION f_instrument_data_strong_signals()
        RETURNS TRIGGER AS $$
        BEGIN
          INSERT INTO instrument_signals (instrument_code, candle_time, price, min_30, timeframe)
          SELECT
            NEW.instrument_code,
            NEW.candle_time,
            NEW.price,
            v.action,
            v.tf
          FROM (VALUES
            ('30m', NEW.min_30),
            ('1h',  NEW.hourly),
            ('5h',  NEW.hours_5),
            ('1d',  NEW.daily),
            ('1w',  NEW.weekly),
            ('1mo', NEW.monthly)
          ) AS v(tf, action)
          WHERE v.action IN ('STRONG BUY', 'STRONG SELL')
          ON CONFLICT (instrument_code, timeframe, candle_time) DO NOTHING;

          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    cur.execute("DROP TRIGGER IF EXISTS trg_instrument_data_strong_signals ON instrument_data;")

    cur.execute(
        """
        CREATE TRIGGER trg_instrument_data_strong_signals
        AFTER INSERT ON instrument_data
        FOR EACH ROW
        EXECUTE FUNCTION f_instrument_data_strong_signals();
        """
    )


def drop_and_recreate_schema() -> None:
    """
    Жёсткий сброс: удаляем прикладные таблицы и создаём заново актуальную схему.
    Использовать только в режиме DB_INIT_MODE=RESET.
    """
    conn = _get_conn()
    if not conn:
        raise RuntimeError("drop_and_recreate_schema: нет соединения с БД")

    try:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS instrument_data CASCADE;")
            cur.execute("DROP TABLE IF EXISTS instrument_signals CASCADE;")
            cur.execute("DROP TABLE IF EXISTS position_state CASCADE;")
            cur.execute("DROP TABLE IF EXISTS instruments CASCADE;")
            cur.execute("DROP TABLE IF EXISTS app_state CASCADE;")
        conn.commit()
    finally:
        conn.close()

    # Создаём заново
    ensure_schema()


def backfill_strong_signals() -> int:
    """
    Восстанавливает instrument_signals из уже существующего instrument_data.
    Безопасно выполнять многократно: дубли не создаст из-за UNIQUE.
    Возвращает количество вставленных строк.
    """
    conn = _get_conn()
    if not conn:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO instrument_signals (instrument_code, candle_time, price, min_30, timeframe)
                SELECT d.instrument_code, d.candle_time, d.price, v.action, v.tf
                FROM instrument_data d
                JOIN LATERAL (VALUES
                  ('30m', d.min_30),
                  ('1h',  d.hourly),
                  ('5h',  d.hours_5),
                  ('1d',  d.daily),
                  ('1w',  d.weekly),
                  ('1mo', d.monthly)
                ) AS v(tf, action) ON TRUE
                WHERE v.action IN ('STRONG BUY', 'STRONG SELL')
                ON CONFLICT (instrument_code, timeframe, candle_time) DO NOTHING;
                """
            )
            inserted = cur.rowcount or 0
        conn.commit()
        return inserted
    except Exception:
        logger.exception("Ошибка backfill_strong_signals")
        return 0
    finally:
        conn.close()


# ----- App state helpers

def is_initialized() -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_state WHERE key='initialized' LIMIT 1;")
            row = cur.fetchone()
            return bool(row and row[0] == "1")
    except Exception:
        logger.exception("Ошибка чтения app_state.initialized")
        return False
    finally:
        conn.close()


def mark_initialized() -> None:
    conn = _get_conn()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_state(key, value)
                VALUES('initialized', '1')
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
                """
            )
        conn.commit()
    finally:
        conn.close()


def clear_initialized_flag() -> None:
    conn = _get_conn()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app_state WHERE key='initialized';")
        conn.commit()
    finally:
        conn.close()


# ----- Data operations

def reset_all_data() -> None:
    """
    Полный сброс прикладных данных (таблицы остаются).
    Используется в режиме DB_INIT_MODE=RESET, если не хочется DROP TABLE.
    """
    conn = _get_conn()
    if not conn:
        raise RuntimeError("reset_all_data: нет соединения с БД")
    try:
        with conn.cursor() as cur:
            # порядок не критичен: CASCADE снимет зависимости
            cur.execute("TRUNCATE instrument_data RESTART IDENTITY CASCADE;")
            cur.execute("TRUNCATE instrument_signals RESTART IDENTITY CASCADE;")
            cur.execute("TRUNCATE position_state RESTART IDENTITY CASCADE;")
            cur.execute("TRUNCATE instruments RESTART IDENTITY CASCADE;")
            cur.execute("TRUNCATE app_state RESTART IDENTITY CASCADE;")
        conn.commit()
    finally:
        conn.close()

def _default_strategy_key_for_code(code: str, plus500_name: Optional[str] = None) -> str:
    code_s = str(code or "").strip()

    mapping = {
        "GBPUSD": "GBP/USD",
        "EURGBP": "EUR/GBP",
        "AUDUSD": "AUD/USD",
        "EURUSD": "EUR/USD",
        "USDJPY": "USD/JPY",
        "USDCAD": "USD/CAD",
        "S&P500": "S&P 500",
        "BITCOIN": "BITCOIN",
        "SOLBTC": "SOL/BTC",
        "EURCHF": "EUR/CHF",
        "AUDNZD": "AUD/NZD",
        "CHFJPY": "CHF/JPY",
    }

    if code_s in mapping:
        return mapping[code_s]

    plus500_s = str(plus500_name or "").strip()
    if plus500_s:
        return plus500_s

    return code_s


def upsert_instrument(
    code: str,
    sheet_name: str,
    plus500_name: Optional[str],
    signals_enabled: bool,
    strategy_key: Optional[str] = None,
    display_name: Optional[str] = None,
    is_active: bool = True,
) -> None:
    conn = _get_conn()
    if not conn:
        return

    strategy_key = str(strategy_key or _default_strategy_key_for_code(code, plus500_name)).strip()
    display_name = str(display_name or strategy_key or plus500_name or code).strip()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO instruments(
                    code,
                    sheet_name,
                    plus500_name,
                    signals_enabled,
                    strategy_key,
                    display_name,
                    is_active
                )
                VALUES(%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (code) DO UPDATE SET
                    sheet_name=EXCLUDED.sheet_name,
                    plus500_name=EXCLUDED.plus500_name,
                    signals_enabled=EXCLUDED.signals_enabled,
                    strategy_key=COALESCE(NULLIF(EXCLUDED.strategy_key, ''), instruments.strategy_key),
                    display_name=COALESCE(NULLIF(EXCLUDED.display_name, ''), instruments.display_name),
                    is_active=EXCLUDED.is_active
                """,
                (
                    code,
                    sheet_name,
                    plus500_name,
                    bool(signals_enabled),
                    strategy_key,
                    display_name,
                    bool(is_active),
                ),
            )
        conn.commit()
    except errors.UniqueViolation:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT code FROM instruments WHERE sheet_name=%s", (sheet_name,))
            row = cur.fetchone()
        existing_code = row[0] if row else "UNKNOWN"
        raise RuntimeError(
            f"sheet_name '{sheet_name}' уже занят инструментом '{existing_code}'. "
            f"Нельзя назначить этот же sheet_name инструменту '{code}'. "
            f"Исправьте config.py / .env, чтобы каждый инструмент имел уникальный sheet_name и совпадающий code."
        )
    finally:
        conn.close()

def insert_instrument_data(
    instrument_code: str,
    executed_at: str,
    price: Optional[float],
    timeframes: Dict[str, Any],
) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        dt = _parse_dt(executed_at)

        def _action(tf: str) -> Optional[str]:
            obj = timeframes.get(tf) or {}
            return obj.get("action")

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO instrument_data(
                    instrument_code, candle_time, price,
                    min_30, hourly, hours_5, daily, weekly, monthly
                )
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (instrument_code, candle_time) DO UPDATE SET
                    price=EXCLUDED.price,
                    min_30=EXCLUDED.min_30,
                    hourly=EXCLUDED.hourly,
                    hours_5=EXCLUDED.hours_5,
                    daily=EXCLUDED.daily,
                    weekly=EXCLUDED.weekly,
                    monthly=EXCLUDED.monthly
                """,
                (
                    instrument_code,
                    dt,
                    float(price or 0.0),
                    _action("30m"),
                    _action("1h"),
                    _action("5h"),
                    _action("1d"),
                    _action("1w"),
                    _action("1mo"),
                ),
            )
        conn.commit()
        return True
    except Exception:
        logger.exception("Ошибка вставки строки в instrument_data (code=%s)", instrument_code)
        return False
    finally:
        conn.close()


def insert_strong_signal(
    instrument_code: str,
    executed_at: str,
    price: Optional[float],
    min_30: str,
    timeframe: str = "30m",
) -> bool:
    """
    ВНИМАНИЕ:
      При включённом DB_ENABLE_SIGNAL_TRIGGER=1 обычно не требуется,
      потому что instrument_signals будет поддерживаться автоматически из instrument_data.
    Оставлено для совместимости/ручных операций.
    """
    conn = _get_conn()
    if not conn:
        return False
    try:
        dt = _parse_dt(executed_at)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO instrument_signals (instrument_code, candle_time, price, min_30, timeframe)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (instrument_code, timeframe, candle_time) DO NOTHING
                """,
                (instrument_code, dt, float(price or 0.0), min_30, str(timeframe)),
            )
        conn.commit()
        return True
    except Exception:
        logger.exception("Ошибка вставки строки в instrument_signals (code=%s)", instrument_code)
        return False
    finally:
        conn.close()


def get_last_strong_signal(instrument_code: str, timeframe: str = "30m") -> Optional[str]:
    conn = _get_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT min_30
                FROM instrument_signals
                WHERE instrument_code=%s
                  AND timeframe=%s
                  AND min_30 IN ('STRONG BUY','STRONG SELL')
                ORDER BY candle_time DESC
                LIMIT 1
                """,
                (instrument_code, str(timeframe)),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        logger.exception("Ошибка чтения последнего сильного сигнала (code=%s)", instrument_code)
        return None
    finally:
        conn.close()

def get_last_strong_signal_before(
    instrument_code: str,
    *,
    timeframe: str = "30m",
    before_executed_at: Union[str, datetime],
) -> Optional[str]:
    """
    Возвращает последний STRONG BUY/SELL по (code, timeframe) со временем candle_time < before_executed_at.
    Нужна для корректного flip/first, когда текущий слот уже записан в БД.
    """
    conn = _get_conn()
    if not conn:
        return None

    try:
        before_dt = _parse_dt(before_executed_at)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT min_30
                FROM instrument_signals
                WHERE instrument_code=%s
                  AND timeframe=%s
                  AND candle_time < %s
                  AND min_30 IN ('STRONG BUY','STRONG SELL')
                ORDER BY candle_time DESC
                LIMIT 1
                """,
                (instrument_code, str(timeframe), before_dt),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        logger.exception("Ошибка чтения предыдущего сильного сигнала (code=%s, tf=%s)", instrument_code, timeframe)
        return None
    finally:
        conn.close()

def get_position_state(instrument_code: str) -> str:
    conn = _get_conn()
    if not conn:
        return "NONE"
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT state
                FROM position_state
                WHERE instrument_code=%s
                ORDER BY id DESC
                LIMIT 1
                """,
                (instrument_code,),
            )
            row = cur.fetchone()
            return row[0] if row else "NONE"
    except Exception:
        logger.exception("Ошибка чтения position_state (code=%s)", instrument_code)
        return "NONE"
    finally:
        conn.close()


def set_position_state(instrument_code: str, state: str, comment: Optional[str] = None) -> None:
    conn = _get_conn()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO position_state(instrument_code, state, comment)
                VALUES(%s,%s,%s)
                """,
                (instrument_code, str(state), comment),
            )
        conn.commit()
    finally:
        conn.close()


# ---- Trade schema v2 helpers (per timeframe)
def get_active_position_for_tf(instrument_code: str, position_tf: str) -> Optional[Dict[str, Any]]:
    """Последняя активная позиция (LONG/SHORT) для instrument+TF."""
    conn = _get_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, state, position_num, opened_at, position_tf, owner_tf, entry_rule
                FROM position_state
                WHERE instrument_code=%s
                  AND position_tf=%s
                  AND state IN ('LONG','SHORT')
                ORDER BY id DESC
                LIMIT 1
                """,
                (instrument_code, position_tf),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "state": row[1],
                "position_num": row[2],
                "opened_at": row[3],
                "position_tf": row[4],
                "owner_tf": row[5],
                "entry_rule": row[6],
            }
    except Exception:
        logger.exception("Ошибка чтения active position_state (code=%s tf=%s)", instrument_code, position_tf)
        return None
    finally:
        conn.close()


def set_position_state_tf(
    instrument_code: str,
    state: str,
    position_tf: str,
    comment: Optional[str] = None,
    position_num: Optional[int] = None,
    opened_at: Optional[Union[str, datetime]] = None,
    entry_rule: Optional[str] = None,
    owner_tf: Optional[str] = None,   # <-- НОВОЕ
) -> Optional[int]:
    """
    Добавляет запись в position_state.
    Возвращает id вставленной записи (или None при ошибке).
    """
    conn = _get_conn()
    if not conn:
        return None

    position_tf = str(position_tf or "").strip().lower()
    owner_tf = str(owner_tf or position_tf).strip().lower()  # <-- если не задано, владелец = tf
    entry_rule = str(entry_rule).strip().upper() if entry_rule else None

    try:
        opened_at_dt: Optional[datetime] = None
        if opened_at:
            opened_at_dt = _parse_dt(opened_at) if isinstance(opened_at, str) else opened_at

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO position_state(
                    instrument_code, state, comment,
                    position_num, opened_at, position_tf, entry_rule, owner_tf
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (
                    instrument_code,
                    state,
                    comment,
                    position_num,
                    opened_at_dt,
                    position_tf,
                    entry_rule,
                    owner_tf,
                ),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
        return int(new_id)

    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception(
            "Ошибка вставки position_state (code=%s tf=%s state=%s entry_rule=%s)",
            instrument_code, position_tf, state, entry_rule
        )
        return None
    finally:
        conn.close()


def drop_trade_schema_v2_tables(cur) -> None:
    """
    Удаляет таблицы схемы v2, если они уже существуют.
    Вызывать внутри транзакции (через conn.cursor()).
    """
    # порядок важен: trade_events ссылается на positions
    cur.execute("DROP TABLE IF EXISTS trade_events CASCADE;")
    cur.execute("DROP TABLE IF EXISTS positions CASCADE;")
    cur.execute("DROP TABLE IF EXISTS last_strong CASCADE;")
    cur.execute("DROP TABLE IF EXISTS strategy_types CASCADE;")

    # сброс маркера версии миграции (если он есть)
    cur.execute("DELETE FROM app_state WHERE key='schema_version';")


def drop_trade_schema_v2_now() -> None:
    """
    Подключается к БД через ваш _get_conn() и удаляет таблицы.
    """
    conn = _get_conn()
    if not conn:
        raise RuntimeError("Нет соединения с БД (_get_conn() вернул None)")
    try:
        with conn.cursor() as cur:
            drop_trade_schema_v2_tables(cur)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def close_position_by_id(position_id: int, comment: str) -> bool:
    """
    Закрывает (UPDATE) существующую активную позицию.
    Возвращает True если реально обновили строку (то есть она была LONG/SHORT).
    """
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE position_state
                SET state='CLOSED',
                    comment=%s,
                    closed_at=NOW()
                WHERE id=%s
                  AND state IN ('LONG','SHORT')
                """,
                (comment, int(position_id)),
            )
            updated = (cur.rowcount or 0) > 0
        conn.commit()
        return updated
    except Exception:
        logger.exception("Ошибка close_position_by_id(id=%s)", position_id)
        return False
    finally:
        conn.close()

def close_active_position_for_tf(instrument_code: str, position_tf: str, comment: str) -> bool:
    """
    Атомарно закрывает последнюю активную позицию (LONG/SHORT) для instrument+TF.
    True если реально закрыли, False если активной позиции не было/уже закрыта.
    """
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH last_active AS (
                  SELECT id
                  FROM position_state
                  WHERE instrument_code=%s
                    AND position_tf=%s
                    AND state IN ('LONG','SHORT')
                  ORDER BY id DESC
                  LIMIT 1
                )
                UPDATE position_state p
                SET state='CLOSED',
                    comment=%s,
                    closed_at=NOW()
                FROM last_active a
                WHERE p.id=a.id
                  AND p.state IN ('LONG','SHORT')
                """,
                (instrument_code, position_tf, comment),
            )
            updated = (cur.rowcount or 0) > 0
        conn.commit()
        return updated
    except Exception:
        logger.exception("Ошибка close_active_position_for_tf(code=%s tf=%s)", instrument_code, position_tf)
        return False
    finally:
        conn.close()

def get_active_position(instrument_code: str) -> Optional[Dict[str, Any]]:
    """Единственная активная позиция на инструмент (LONG/SHORT)."""
    conn = _get_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, state, position_num, opened_at, closed_at, position_tf, entry_rule, owner_tf
                FROM position_state
                WHERE instrument_code=%s
                  AND state IN ('LONG','SHORT')
                ORDER BY id DESC
                LIMIT 1
                """,
                (instrument_code,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "state": row[1],
                "position_num": row[2],
                "opened_at": row[3],
                "closed_at": row[4],
                "position_tf": row[5],
                "entry_rule": row[6],
                "owner_tf": row[7],
            }
    except Exception:
        logger.exception("Ошибка чтения active position_state (code=%s)", instrument_code)
        return None
    finally:
        conn.close()


def handoff_position_owner(position_id: int, new_owner_tf: str, new_entry_rule: str, comment: str) -> bool:
    """Переписать владельца позиции и правило входа (handoff) без закрытия."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE position_state
                SET owner_tf=%s,
                    entry_rule=%s,
                    comment=%s
                WHERE id=%s
                  AND state IN ('LONG','SHORT')
                """,
                (str(new_owner_tf).strip().lower(), str(new_entry_rule).strip().upper(), comment, int(position_id)),
            )
            updated = (cur.rowcount or 0) > 0
        conn.commit()
        return updated
    except Exception:
        logger.exception("Ошибка handoff_position_owner(id=%s)", position_id)
        return False
    finally:
        conn.close()

def get_active_position_for_owner_tf(instrument_code: str, owner_tf: str):
    conn = _get_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, state, position_num, opened_at, position_tf, owner_tf, entry_rule
                FROM position_state
                WHERE instrument_code=%s
                  AND state IN ('LONG','SHORT')
                  AND owner_tf=%s
                ORDER BY id DESC
                LIMIT 1
                """,
                (instrument_code, str(owner_tf).strip().lower()),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "state": row[1],
                "position_num": row[2],
                "opened_at": row[3],
                "position_tf": row[4],
                "owner_tf": row[5],
                "entry_rule": row[6],
            }
    except Exception:
        logger.exception("Ошибка get_active_position_for_owner_tf(%s,%s)", instrument_code, owner_tf)
        return None
    finally:
        conn.close()

from datetime import timedelta

def has_position_today(
    instrument_code: str,
    position_tf: str,
    day_start: datetime,
    day_end: datetime,
) -> bool:
    """
    True если по instrument_code+position_tf есть любая запись (включая CLOSED),
    у которой opened_at (или created_at если opened_at NULL) попадает в [day_start, day_end).
    Нужна чтобы отличать 'первый вход в день' от 'режим в течение дня'.
    """
    conn = _get_conn()
    if not conn:
        return False
    try:
        position_tf = str(position_tf or "").strip().lower()
        day_start = day_start.replace(tzinfo=None)
        day_end = day_end.replace(tzinfo=None)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM position_state
                WHERE instrument_code=%s
                  AND position_tf=%s
                  AND COALESCE(opened_at, created_at) >= %s
                  AND COALESCE(opened_at, created_at) < %s
                LIMIT 1
                """,
                (instrument_code, position_tf, day_start, day_end),
            )
            return cur.fetchone() is not None
    except Exception:
        logger.exception("Ошибка has_position_today(code=%s tf=%s)", instrument_code, position_tf)
        return False
    finally:
        conn.close()

def handoff_position_to_tf(position_id: int, new_tf: str, new_entry_rule: str, comment: str) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE position_state
                SET position_tf=%s,
                    owner_tf=%s,
                    entry_rule=%s,
                    comment=%s
                WHERE id=%s
                  AND state IN ('LONG','SHORT')
                """,
                (new_tf.strip().lower(), new_tf.strip().lower(), new_entry_rule.strip().upper(), comment, int(position_id)),
            )
            updated = (cur.rowcount or 0) > 0
        conn.commit()
        return updated
    except Exception:
        logger.exception("Ошибка handoff_position_to_tf(id=%s)", position_id)
        return False
    finally:
        conn.close()

def swap_handoff_positions(
    pos_id_a: int,
    new_tf_a: str,
    new_rule_a: str,
    comment_a: str,
    pos_id_b: int,
    new_tf_b: str,
    new_rule_b: str,
    comment_b: str,
) -> bool:
    """
    Атомарный swap двух активных позиций (LONG/SHORT) между слотами (TF).
    Делается в одной транзакции: либо обе UPDATE пройдут, либо откат.
    """
    conn = _get_conn()
    if not conn:
        return False

    try:
        with conn.cursor() as cur:
            # позиция A -> новый TF
            cur.execute(
                """
                UPDATE position_state
                SET position_tf=%s,
                    owner_tf=%s,
                    entry_rule=%s,
                    comment=%s
                WHERE id=%s
                  AND state IN ('LONG','SHORT')
                """,
                (
                    new_tf_a.strip().lower(),
                    new_tf_a.strip().lower(),
                    str(new_rule_a).strip().upper(),
                    comment_a,
                    int(pos_id_a),
                ),
            )
            if (cur.rowcount or 0) != 1:
                conn.rollback()
                return False

            # позиция B -> новый TF
            cur.execute(
                """
                UPDATE position_state
                SET position_tf=%s,
                    owner_tf=%s,
                    entry_rule=%s,
                    comment=%s
                WHERE id=%s
                  AND state IN ('LONG','SHORT')
                """,
                (
                    new_tf_b.strip().lower(),
                    new_tf_b.strip().lower(),
                    str(new_rule_b).strip().upper(),
                    comment_b,
                    int(pos_id_b),
                ),
            )
            if (cur.rowcount or 0) != 1:
                conn.rollback()
                return False

        conn.commit()
        return True

    except Exception:
        logger.exception("Ошибка swap_handoff_positions(a=%s b=%s)", pos_id_a, pos_id_b)
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()



# ----- Strategy DB helpers

_ALLOWED_STRATEGY_SIGNALS = {
    "BUY_ON_STRONG_BUY",
    "BUY_ON_STRONG_SELL",
    "SELL_ON_STRONG_BUY",
    "SELL_ON_STRONG_SELL",
}

_ALLOWED_DB_TIMEFRAMES = {"30m", "1h", "5h", "1d", "1w", "1mo"}

_TF_API_TO_DB = {
    "30m": "30m",
    "1h": "1h",
    "5h": "5h",
    "day": "1d",
    "1d": "1d",
    "week": "1w",
    "1w": "1w",
    "month": "1mo",
    "1mo": "1mo",
}

_TF_DB_TO_API = {
    "30m": "30m",
    "1h": "1h",
    "5h": "5h",
    "1d": "day",
    "1w": "week",
    "1mo": "month",
}

_DEFAULT_TF_PRIORITY = ["30m", "1h", "5h", "1d", "1w", "1mo"]

_DEFAULT_STRATEGY_KEYS = {
    "GBPUSD": "GBP/USD",
    "EURGBP": "EUR/GBP",
    "AUDUSD": "AUD/USD",
    "EURUSD": "EUR/USD",
    "USDJPY": "USD/JPY",
    "USDCAD": "USD/CAD",
    "S&P500": "S&P 500",
    "BITCOIN": "BITCOIN",
    "SOLBTC": "SOL/BTC",
    "EURCHF": "EUR/CHF",
    "AUDNZD": "AUD/NZD",
    "CHFJPY": "CHF/JPY",
}


def normalize_strategy_timeframe(value: str) -> str:
    raw = str(value or "").strip()
    key = raw.lower()

    if key not in _TF_API_TO_DB:
        raise ValueError(
            f"Bad timeframe={value!r}. Allowed: 30m, 1h, 5h, day, week, month"
        )

    return _TF_API_TO_DB[key]


def strategy_timeframe_to_api(value: str) -> str:
    key = str(value or "").strip().lower()
    return _TF_DB_TO_API.get(key, key)


def normalize_strategy_signal(value: str) -> str:
    signal = str(value or "").strip().upper()

    if signal not in _ALLOWED_STRATEGY_SIGNALS:
        raise ValueError(
            f"Bad signal={value!r}. Allowed: {sorted(_ALLOWED_STRATEGY_SIGNALS)}"
        )

    return signal


def _default_strategy_key_for_code(code: str, plus500_name: Optional[str] = None) -> str:
    code_s = str(code or "").strip()
    if code_s in _DEFAULT_STRATEGY_KEYS:
        return _DEFAULT_STRATEGY_KEYS[code_s]

    plus500_s = str(plus500_name or "").strip()
    if plus500_s:
        return plus500_s

    return code_s


def _hhmm_to_min_for_strategy(value: str) -> int:
    s = str(value or "").strip()

    if s == "24:00":
        return 1440

    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"Bad time format={value!r}. Expected HH:MM")

    hh = int(parts[0])
    mm = int(parts[1])

    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        raise ValueError(f"Bad time value={value!r}")

    return hh * 60 + mm


def _strategy_time_in_window(now_dt, start_time: str, end_time: str) -> bool:
    try:
        start_min = _hhmm_to_min_for_strategy(start_time)
        end_min = _hhmm_to_min_for_strategy(end_time)
        now_min = int(now_dt.hour) * 60 + int(now_dt.minute)

        if start_min <= end_min:
            return start_min <= now_min < end_min

        # окно через полночь
        return now_min >= start_min or now_min < end_min

    except Exception:
        logger.exception(
            "Bad strategy time window: start=%s end=%s",
            start_time,
            end_time,
        )
        return False


def get_tf_priority() -> list[str]:
    conn = _get_conn()
    if not conn:
        return _DEFAULT_TF_PRIORITY.copy()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT value_json
                FROM strategy_settings
                WHERE key='tf_priority'
                LIMIT 1
                """
            )
            row = cur.fetchone()

        if not row:
            return _DEFAULT_TF_PRIORITY.copy()

        value = row[0]

        if isinstance(value, str):
            value = json.loads(value)

        if not isinstance(value, list):
            return _DEFAULT_TF_PRIORITY.copy()

        out: list[str] = []

        for item in value:
            try:
                tf = normalize_strategy_timeframe(str(item))
            except Exception:
                continue

            if tf not in out:
                out.append(tf)

        return out or _DEFAULT_TF_PRIORITY.copy()

    except Exception:
        logger.exception("Ошибка чтения strategy_settings.tf_priority")
        return _DEFAULT_TF_PRIORITY.copy()
    finally:
        conn.close()


def set_tf_priority(timeframes: list[str]) -> list[str]:
    normalized: list[str] = []

    for item in timeframes or []:
        tf = normalize_strategy_timeframe(str(item))
        if tf not in normalized:
            normalized.append(tf)

    if not normalized:
        raise ValueError("tf_priority cannot be empty")

    conn = _get_conn()
    if not conn:
        raise RuntimeError("set_tf_priority: нет соединения с БД")

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO strategy_settings(key, value_json)
                VALUES('tf_priority', %s::jsonb)
                ON CONFLICT (key) DO UPDATE SET
                    value_json=EXCLUDED.value_json,
                    updated_at=NOW()
                """,
                (json.dumps(normalized),),
            )

        conn.commit()
        return normalized
    finally:
        conn.close()


# def get_allowed_strategy_rules_by_tf(instrument_code: str, now_dt) -> Dict[str, list[str]]:
#     """
#     Возвращает формат, совместимый с phase1_logic._allowed_types_by_tf():
#
#     {
#         "30m": ["BUY_ON_STRONG_BUY", "SELL_ON_STRONG_SELL"],
#         "1h": ["BUY_ON_STRONG_SELL"]
#     }
#
#     Дубли НЕ удаляются.
#     Если у дня нет активных строк или текущее время вне start/end — вернёт {}.
#     """
#     code = str(instrument_code or "").strip()
#     day_of_week = int(now_dt.weekday())
#
#     conn = _get_conn()
#     if not conn:
#         return {}
#
#     try:
#         with conn.cursor() as cur:
#             cur.execute(
#                 """
#                 SELECT
#                     r.timeframe,
#                     r.signal,
#                     r.start_time,
#                     r.end_time
#                 FROM strategy_rules r
#                 JOIN instruments i ON i.code = r.instrument_code
#                 WHERE r.instrument_code = %s
#                   AND r.day_of_week = %s
#                   AND r.is_active = TRUE
#                   AND COALESCE(i.is_active, TRUE) = TRUE
#                   AND COALESCE(i.signals_enabled, TRUE) = TRUE
#                 ORDER BY r.row_order ASC, r.id ASC
#                 """,
#                 (code, day_of_week),
#             )
#             rows = cur.fetchall()
#
#         out: Dict[str, list[str]] = {}
#
#         for timeframe, signal, start_time, end_time in rows:
#             if not _strategy_time_in_window(
#                 now_dt,
#                 str(start_time or "00:00"),
#                 str(end_time or "24:00"),
#             ):
#                 continue
#
#             tf = normalize_strategy_timeframe(str(timeframe))
#             sig = normalize_strategy_signal(str(signal))
#
#             out.setdefault(tf, []).append(sig)
#
#         return out
#
#     except Exception:
#         logger.exception("Ошибка чтения strategy_rules для code=%s", code)
#         return {}
#     finally:
#         conn.close()

def get_allowed_strategy_rules_by_tf(instrument_code: str, now_dt) -> Dict[str, list[dict]]:
    """
    Возвращает активные правила по TF.

    Формат:

    {
        "30m": [
            {
                "signal": "BUY_ON_STRONG_BUY",
                "skip": 0,
                "trade_amount": 100.0,
            }
        ]
    }
    """
    code = str(instrument_code or "").strip()
    day_of_week = int(now_dt.weekday())

    conn = _get_conn()
    if not conn:
        return {}

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    r.timeframe,
                    r.signal,
                    r.skip_count,
                    r.trade_amount,
                    r.start_time,
                    r.end_time
                FROM strategy_rules r
                JOIN instruments i
                  ON i.code = r.instrument_code
                WHERE r.instrument_code = %s
                  AND r.day_of_week = %s
                  AND r.is_active = TRUE
                  AND COALESCE(i.is_active, TRUE) = TRUE
                  AND COALESCE(i.signals_enabled, TRUE) = TRUE
                ORDER BY r.row_order ASC, r.id ASC
                """,
                (code, day_of_week),
            )

            rows = cur.fetchall()
            logger.info(
                "STRATEGY %s: rows_from_db=%s day=%s",
                code,
                len(rows),
                day_of_week,
            )

        out: Dict[str, list[dict]] = {}

        for timeframe, signal, skip_count, trade_amount, start_time, end_time in rows:

            if not _strategy_time_in_window(
                now_dt,
                str(start_time or "00:00"),
                str(end_time or "24:00"),
            ):
                continue

            tf = normalize_strategy_timeframe(str(timeframe))

            out.setdefault(tf, []).append({
                "signal": normalize_strategy_signal(str(signal)),
                "skip": int(skip_count or 0),
                "trade_amount": float(trade_amount) if trade_amount is not None else None,
            })

        return out

    except Exception:
        logger.exception("Ошибка чтения strategy_rules для code=%s", code)
        return {}

    finally:
        conn.close()

def list_strategy_instruments() -> list[dict]:
    conn = _get_conn()
    if not conn:
        return []

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    i.code,
                    i.strategy_key,
                    i.display_name,
                    i.plus500_name,
                    i.signals_enabled,
                    COALESCE(i.is_active, TRUE) AS is_active,
                    COUNT(r.id) FILTER (WHERE r.is_active = TRUE) AS rules_count
                FROM instruments i
                LEFT JOIN strategy_rules r ON r.instrument_code = i.code
                GROUP BY
                    i.code,
                    i.strategy_key,
                    i.display_name,
                    i.plus500_name,
                    i.signals_enabled,
                    i.is_active
                ORDER BY COALESCE(i.strategy_key, i.plus500_name, i.code)
                """
            )
            rows = cur.fetchall()

        return [
            {
                "code": row[0],
                "strategy_key": row[1],
                "display_name": row[2],
                "plus500_name": row[3],
                "signals_enabled": bool(row[4]),
                "is_active": bool(row[5]),
                "rules_count": int(row[6] or 0),
            }
            for row in rows
        ]

    except Exception:
        logger.exception("Ошибка чтения списка strategy instruments")
        return []
    finally:
        conn.close()


def get_strategy_instrument_detail(strategy_key: str) -> Optional[dict]:
    key = str(strategy_key or "").strip()

    conn = _get_conn()
    if not conn:
        return None

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    code,
                    strategy_key,
                    display_name,
                    plus500_name,
                    signals_enabled,
                    COALESCE(is_active, TRUE)
                FROM instruments
                WHERE strategy_key = %s
                   OR code = %s
                   OR plus500_symbol = %s
                LIMIT 1
                """,
                (key, key, key),
            )
            instrument = cur.fetchone()

            if not instrument:
                return None

            code = instrument[0]

            cur.execute(
                """
                SELECT
                    id,
                    day_of_week,
                    row_order,
                    start_time,
                    end_time,
                    timeframe,
                    signal,
                    skip_count,
                    trade_amount,
                    is_active
                FROM strategy_rules
                WHERE instrument_code = %s
                  AND is_active = TRUE
                ORDER BY day_of_week ASC, row_order ASC, id ASC
                """,
                (code,),
            )
            rules = cur.fetchall()

        week = {str(i): [] for i in range(7)}

        for row in rules:
            day_key = str(int(row[1]))
            week[day_key].append(
                {
                    "id": int(row[0]),
                    "day_of_week": int(row[1]),
                    "row_order": int(row[2] or 0),
                    "start": row[3],
                    "end": row[4],
                    "timeframe": strategy_timeframe_to_api(row[5]),
                    "timeframe_db": row[5],
                    "signal": row[6],
                    "skip": int(row[7] or 0),
                    "trade_amount": float(row[8]) if row[8] is not None else None,
                    "is_active": bool(row[9]),
                }
            )

        return {
            "code": instrument[0],
            "strategy_key": instrument[1],
            "display_name": instrument[2],
            "plus500_name": instrument[3],
            "signals_enabled": bool(instrument[4]),
            "is_active": bool(instrument[5]),
            "week": week,
        }

    except Exception:
        logger.exception("Ошибка чтения strategy instrument detail key=%s", key)
        return None
    finally:
        conn.close()


def export_strategy_json_from_db() -> dict:
    """
    Собирает JSON, совместимый со старым SIGNAL_STRATEGY_JSON.

    Важно:
    - ключи instruments остаются техническими code: GBPUSD, EURGBP, S&P500;
    - пустой день отдаётся как PAUSE;
    - дубли сигналов сохраняются.
    """
    conn = _get_conn()
    if not conn:
        return {
            "tf_priority": _DEFAULT_TF_PRIORITY.copy(),
            "instruments": {
                "DEFAULT": {
                    "week": {str(i): {"mode": ["PAUSE"]} for i in range(7)}
                }
            },
        }

    try:
        tf_priority = get_tf_priority()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT code
                FROM instruments
                WHERE COALESCE(is_active, TRUE) = TRUE
                  AND COALESCE(signals_enabled, TRUE) = TRUE
                ORDER BY code ASC
                """
            )
            codes = [row[0] for row in cur.fetchall()]

            cur.execute(
                """
                SELECT
                    instrument_code,
                    day_of_week,
                    row_order,
                    start_time,
                    end_time,
                    timeframe,
                    signal
                FROM strategy_rules
                WHERE is_active = TRUE
                ORDER BY instrument_code ASC, day_of_week ASC, row_order ASC, id ASC
                """
            )
            rules = cur.fetchall()

        instruments: dict[str, dict] = {}

        for code in codes:
            instruments[code] = {
                "week": {str(i): {"mode": ["PAUSE"]} for i in range(7)}
            }

        for code, day, _row_order, start, end, timeframe, signal in rules:
            if code not in instruments:
                continue

            day_key = str(int(day))
            day_cfg = instruments[code]["week"].setdefault(day_key, {})

            if day_cfg.get("mode") == ["PAUSE"]:
                day_cfg.clear()

            day_cfg.setdefault("start", start or "00:00")
            day_cfg.setdefault("end", end or "24:00")
            day_cfg["mode"] = ["SIGNAL"]

            tf = normalize_strategy_timeframe(str(timeframe))
            sig = normalize_strategy_signal(str(signal))

            day_cfg.setdefault(tf, []).append(sig)

        instruments["DEFAULT"] = {
            "week": {str(i): {"mode": ["PAUSE"]} for i in range(7)}
        }

        return {
            "tf_priority": tf_priority,
            "instruments": instruments,
        }

    except Exception:
        logger.exception("Ошибка export_strategy_json_from_db")
        return {
            "tf_priority": _DEFAULT_TF_PRIORITY.copy(),
            "instruments": {
                "DEFAULT": {
                    "week": {str(i): {"mode": ["PAUSE"]} for i in range(7)}
                }
            },
        }
    finally:
        conn.close()



# ----- Plus500 instrument metadata helpers

_PLUS500_INSTRUMENT_METADATA_DEFAULTS = {
    "GBPUSD": {
        "instrument_kind": "FOREX",
        "strategy_base_currency": "GBP",
        "strategy_quote_currency": "USD",
        "plus500_base_currency": "GBP",
        "plus500_quote_currency": "USD",
        "plus500_name": "GBP/USD",
        "plus500_symbol": "GBPUSD",
        "amount_unit": "BASE_CURRENCY",
        "amount_currency": "GBP",
        "price_currency": "USD",
        "margin_currency": "USD",
        "plus500_trade_enabled": True,
        "plus500_details_url": "https://www.plus500.com/en/instruments/gbpusd",
    },
    "EURGBP": {
        "instrument_kind": "FOREX",
        "strategy_base_currency": "EUR",
        "strategy_quote_currency": "GBP",
        "plus500_base_currency": "EUR",
        "plus500_quote_currency": "GBP",
        "plus500_name": "EUR/GBP",
        "plus500_symbol": "EURGBP",
        "amount_unit": "BASE_CURRENCY",
        "amount_currency": "EUR",
        "price_currency": "GBP",
        "margin_currency": "GBP",
        "plus500_trade_enabled": True,
        "plus500_details_url": "https://www.plus500.com/en/instruments/eurgbp",
    },
    "AUDUSD": {
        "instrument_kind": "FOREX",
        "strategy_base_currency": "AUD",
        "strategy_quote_currency": "USD",
        "plus500_base_currency": "AUD",
        "plus500_quote_currency": "USD",
        "plus500_name": "AUD/USD",
        "plus500_symbol": "AUDUSD",
        "amount_unit": "BASE_CURRENCY",
        "amount_currency": "AUD",
        "price_currency": "USD",
        "margin_currency": "USD",
        "plus500_trade_enabled": True,
        "plus500_details_url": "https://www.plus500.com/en/instruments/audusd",
    },
    "EURUSD": {
        "instrument_kind": "FOREX",
        "strategy_base_currency": "EUR",
        "strategy_quote_currency": "USD",
        "plus500_base_currency": "EUR",
        "plus500_quote_currency": "USD",
        "plus500_name": "EUR/USD",
        "plus500_symbol": "EURUSD",
        "amount_unit": "BASE_CURRENCY",
        "amount_currency": "EUR",
        "price_currency": "USD",
        "margin_currency": "USD",
        "plus500_trade_enabled": True,
        "plus500_details_url": "https://www.plus500.com/en/instruments/eurusd",
    },
    "USDJPY": {
        "instrument_kind": "FOREX",
        "strategy_base_currency": "USD",
        "strategy_quote_currency": "JPY",
        "plus500_base_currency": "USD",
        "plus500_quote_currency": "JPY",
        "plus500_name": "USD/JPY",
        "plus500_symbol": "USDJPY",
        "amount_unit": "BASE_CURRENCY",
        "amount_currency": "USD",
        "price_currency": "JPY",
        "margin_currency": "JPY",
        "plus500_trade_enabled": True,
        "plus500_details_url": "https://www.plus500.com/en/instruments/usdjpy",
    },
    "USDCAD": {
        "instrument_kind": "FOREX",
        "strategy_base_currency": "USD",
        "strategy_quote_currency": "CAD",
        "plus500_base_currency": "USD",
        "plus500_quote_currency": "CAD",
        "plus500_name": "USD/CAD",
        "plus500_symbol": "USDCAD",
        "amount_unit": "BASE_CURRENCY",
        "amount_currency": "USD",
        "price_currency": "CAD",
        "margin_currency": "CAD",
        "plus500_trade_enabled": True,
        "plus500_details_url": "https://www.plus500.com/en/instruments/usdcad",
    },
    "S&P500": {
        "instrument_kind": "INDEX",
        "strategy_base_currency": None,
        "strategy_quote_currency": "USD",
        "plus500_base_currency": None,
        "plus500_quote_currency": "USD",
        "plus500_name": "S&P 500",
        "plus500_symbol": "ES",
        "amount_unit": "CONTRACT",
        "amount_currency": None,
        "price_currency": "USD",
        "margin_currency": "USD",
        "plus500_trade_enabled": True,
        "plus500_details_url": "https://www.plus500.com/en/instruments/es/",
    },
    "BITCOIN": {
        "instrument_kind": "CRYPTO",
        "strategy_base_currency": "BTC",
        "strategy_quote_currency": "USD",
        "plus500_base_currency": "BTC",
        "plus500_quote_currency": "USD",
        "plus500_name": "Bitcoin",
        "plus500_symbol": "BTCUSD",
        "amount_unit": "BASE_CURRENCY",
        "amount_currency": "BTC",
        "price_currency": "USD",
        "margin_currency": "USD",
        "plus500_trade_enabled": True,
        "plus500_details_url": "https://www.plus500.com/en-de/instruments/btcusd",
    },
    "SOLBTC": {
        "instrument_kind": "CRYPTO",
        "strategy_base_currency": "SOL",
        "strategy_quote_currency": "BTC",
        "plus500_base_currency": "SOL",
        "plus500_quote_currency": "USD",
        "plus500_name": "Solana",
        "plus500_symbol": "SOLUSD",
        "amount_unit": "BASE_CURRENCY",
        "amount_currency": "SOL",
        "price_currency": "USD",
        "margin_currency": "USD",
        "plus500_trade_enabled": True,
        "plus500_details_url": "https://www.plus500.com/en-it/instruments/solusd",
    },
    "EURCHF": {
        "instrument_kind": "FOREX",
        "strategy_base_currency": "EUR",
        "strategy_quote_currency": "CHF",
        "plus500_base_currency": "EUR",
        "plus500_quote_currency": "CHF",
        "plus500_name": "EUR/CHF",
        "plus500_symbol": "EURCHF",
        "amount_unit": "BASE_CURRENCY",
        "amount_currency": "EUR",
        "price_currency": "CHF",
        "margin_currency": "CHF",
        "plus500_trade_enabled": True,
        "plus500_details_url": "https://www.plus500.com/en/instruments/eurchf",
    },
    "AUDNZD": {
        "instrument_kind": "FOREX",
        "strategy_base_currency": "AUD",
        "strategy_quote_currency": "NZD",
        "plus500_base_currency": "AUD",
        "plus500_quote_currency": "NZD",
        "plus500_name": "AUD/NZD",
        "plus500_symbol": "AUDNZD",
        "amount_unit": "BASE_CURRENCY",
        "amount_currency": "AUD",
        "price_currency": "NZD",
        "margin_currency": "NZD",
        "plus500_trade_enabled": True,
        "plus500_details_url": "https://www.plus500.com/en/instruments/audnzd",
    },
    "CHFJPY": {
        "instrument_kind": "FOREX",
        "strategy_base_currency": "CHF",
        "strategy_quote_currency": "JPY",
        "plus500_base_currency": "CHF",
        "plus500_quote_currency": "JPY",
        "plus500_name": "CHF/JPY",
        "plus500_symbol": "CHFJPY",
        "amount_unit": "BASE_CURRENCY",
        "amount_currency": "CHF",
        "price_currency": "JPY",
        "margin_currency": "JPY",
        "plus500_trade_enabled": True,
        "plus500_details_url": "https://www.plus500.com/en/instruments/chfjpy",
    },
}


def get_default_plus500_instrument_metadata() -> dict[str, dict]:
    return {
        code: dict(meta)
        for code, meta in _PLUS500_INSTRUMENT_METADATA_DEFAULTS.items()
    }


def sync_plus500_instrument_metadata(overwrite: bool = False) -> int:
    """
    Безопасная синхронизация metadata по Plus500.

    overwrite=False:
      - заполняет metadata только если plus500_details_updated_at IS NULL;
      - plus500_name заполняет только если он пустой;
      - не трогает уже отредактированные пользователем metadata.

    overwrite=True:
      - перезаписывает стабильные metadata из дефолтной карты.
    """
    conn = _get_conn()
    if not conn:
        raise RuntimeError("sync_plus500_instrument_metadata: нет соединения с БД")

    changed = 0

    try:
        with conn.cursor() as cur:
            for code, meta in _PLUS500_INSTRUMENT_METADATA_DEFAULTS.items():
                params = dict(meta)
                params["code"] = code
                params["overwrite"] = bool(overwrite)

                cur.execute(
                    """
                    UPDATE instruments
                    SET
                        plus500_name = CASE
                            WHEN %(overwrite)s
                              OR plus500_name IS NULL
                              OR plus500_name = ''
                            THEN %(plus500_name)s
                            ELSE plus500_name
                        END,

                        instrument_kind = CASE
                            WHEN %(overwrite)s OR plus500_details_updated_at IS NULL
                            THEN %(instrument_kind)s
                            ELSE instrument_kind
                        END,

                        plus500_symbol = CASE
                            WHEN %(overwrite)s OR plus500_details_updated_at IS NULL
                            THEN %(plus500_symbol)s
                            ELSE plus500_symbol
                        END,

                        strategy_base_currency = CASE
                            WHEN %(overwrite)s OR plus500_details_updated_at IS NULL
                            THEN %(strategy_base_currency)s
                            ELSE strategy_base_currency
                        END,

                        strategy_quote_currency = CASE
                            WHEN %(overwrite)s OR plus500_details_updated_at IS NULL
                            THEN %(strategy_quote_currency)s
                            ELSE strategy_quote_currency
                        END,

                        plus500_base_currency = CASE
                            WHEN %(overwrite)s OR plus500_details_updated_at IS NULL
                            THEN %(plus500_base_currency)s
                            ELSE plus500_base_currency
                        END,

                        plus500_quote_currency = CASE
                            WHEN %(overwrite)s OR plus500_details_updated_at IS NULL
                            THEN %(plus500_quote_currency)s
                            ELSE plus500_quote_currency
                        END,

                        amount_unit = CASE
                            WHEN %(overwrite)s OR plus500_details_updated_at IS NULL
                            THEN %(amount_unit)s
                            ELSE amount_unit
                        END,

                        amount_currency = CASE
                            WHEN %(overwrite)s OR plus500_details_updated_at IS NULL
                            THEN %(amount_currency)s
                            ELSE amount_currency
                        END,

                        price_currency = CASE
                            WHEN %(overwrite)s OR plus500_details_updated_at IS NULL
                            THEN %(price_currency)s
                            ELSE price_currency
                        END,

                        margin_currency = CASE
                            WHEN %(overwrite)s OR plus500_details_updated_at IS NULL
                            THEN %(margin_currency)s
                            ELSE margin_currency
                        END,

                        plus500_trade_enabled = CASE
                            WHEN %(overwrite)s OR plus500_details_updated_at IS NULL
                            THEN %(plus500_trade_enabled)s
                            ELSE plus500_trade_enabled
                        END,

                        plus500_details_url = CASE
                            WHEN %(overwrite)s OR plus500_details_updated_at IS NULL
                            THEN %(plus500_details_url)s
                            ELSE plus500_details_url
                        END,

                        plus500_details_updated_at = CASE
                            WHEN %(overwrite)s OR plus500_details_updated_at IS NULL
                            THEN NOW()
                            ELSE plus500_details_updated_at
                        END
                    WHERE code = %(code)s
                    """,
                    params,
                )

                changed += int(cur.rowcount or 0)

        conn.commit()
        return changed

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def list_plus500_instrument_metadata() -> list[dict]:
    conn = _get_conn()
    if not conn:
        return []

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    code,
                    strategy_key,
                    plus500_name,
                    plus500_symbol,
                    instrument_kind,
                    strategy_base_currency,
                    strategy_quote_currency,
                    plus500_base_currency,
                    plus500_quote_currency,
                    amount_unit,
                    amount_currency,
                    price_currency,
                    margin_currency,
                    plus500_trade_enabled,
                    plus500_min_unit_amount,
                    plus500_initial_margin_pct,
                    plus500_maintenance_margin_pct,
                    plus500_leverage,
                    plus500_details_url,
                    plus500_details_updated_at
                FROM instruments
                ORDER BY COALESCE(strategy_key, code), code
                """
            )
            rows = cur.fetchall()

        out = []
        for row in rows:
            out.append({
                "code": row[0],
                "strategy_key": row[1],
                "plus500_name": row[2],
                "plus500_symbol": row[3],
                "instrument_kind": row[4],
                "strategy_base_currency": row[5],
                "strategy_quote_currency": row[6],
                "plus500_base_currency": row[7],
                "plus500_quote_currency": row[8],
                "amount_unit": row[9],
                "amount_currency": row[10],
                "price_currency": row[11],
                "margin_currency": row[12],
                "plus500_trade_enabled": bool(row[13]),
                "plus500_min_unit_amount": float(row[14]) if row[14] is not None else None,
                "plus500_initial_margin_pct": float(row[15]) if row[15] is not None else None,
                "plus500_maintenance_margin_pct": float(row[16]) if row[16] is not None else None,
                "plus500_leverage": row[17],
                "plus500_details_url": row[18],
                "plus500_details_updated_at": row[19].isoformat(sep=" ") if row[19] else None,
            })

        return out

    except Exception:
        logger.exception("Ошибка чтения Plus500 metadata")
        return []

    finally:
        conn.close()


def get_plus500_instrument_metadata(key_or_code: str) -> Optional[dict]:
    needle = str(key_or_code or "").strip()
    if not needle:
        return None

    conn = _get_conn()
    if not conn:
        return None

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    code,
                    strategy_key,
                    plus500_name,
                    plus500_symbol,
                    instrument_kind,
                    strategy_base_currency,
                    strategy_quote_currency,
                    plus500_base_currency,
                    plus500_quote_currency,
                    amount_unit,
                    amount_currency,
                    price_currency,
                    margin_currency,
                    plus500_trade_enabled,
                    plus500_min_unit_amount,
                    plus500_initial_margin_pct,
                    plus500_maintenance_margin_pct,
                    plus500_leverage,
                    plus500_details_url,
                    plus500_details_updated_at
                FROM instruments
                WHERE code = %s OR strategy_key = %s OR plus500_symbol = %s
                LIMIT 1
                """,
                (needle, needle, needle),
            )
            row = cur.fetchone()

        if not row:
            return None

        return {
            "code": row[0],
            "strategy_key": row[1],
            "plus500_name": row[2],
            "plus500_symbol": row[3],
            "instrument_kind": row[4],
            "strategy_base_currency": row[5],
            "strategy_quote_currency": row[6],
            "plus500_base_currency": row[7],
            "plus500_quote_currency": row[8],
            "amount_unit": row[9],
            "amount_currency": row[10],
            "price_currency": row[11],
            "margin_currency": row[12],
            "plus500_trade_enabled": bool(row[13]),
            "plus500_min_unit_amount": float(row[14]) if row[14] is not None else None,
            "plus500_initial_margin_pct": float(row[15]) if row[15] is not None else None,
            "plus500_maintenance_margin_pct": float(row[16]) if row[16] is not None else None,
            "plus500_leverage": row[17],
            "plus500_details_url": row[18],
            "plus500_details_updated_at": row[19].isoformat(sep=" ") if row[19] else None,
        }

    except Exception:
        logger.exception("Ошибка чтения Plus500 metadata key_or_code=%s", needle)
        return None

    finally:
        conn.close()



# ----- Strategy API write helpers

def _fetch_strategy_rules_snapshot(cur, instrument_code: str) -> list[dict]:
    cur.execute(
        """
        SELECT
            id,
            instrument_code,
            day_of_week,
            row_order,
            start_time,
            end_time,
            timeframe,
            signal,
            is_active
        FROM strategy_rules
        WHERE instrument_code=%s
          AND is_active=TRUE
        ORDER BY day_of_week ASC, row_order ASC, id ASC
        """,
        (instrument_code,),
    )

    out = []
    for row in cur.fetchall():
        out.append({
            "id": int(row[0]),
            "instrument_code": row[1],
            "day_of_week": int(row[2]),
            "row_order": int(row[3] or 0),
            "start": row[4],
            "end": row[5],
            "timeframe": row[6],
            "signal": row[7],
            "is_active": bool(row[8]),
        })

    return out


def _resolve_instrument_by_strategy_key(cur, strategy_key: str) -> Optional[dict]:
    key = str(strategy_key or "").strip()

    if not key:
        return None

    cur.execute(
        """
        SELECT
            code,
            strategy_key,
            display_name,
            plus500_name,
            signals_enabled,
            COALESCE(is_active, TRUE)
        FROM instruments
        WHERE strategy_key=%s OR code=%s OR plus500_symbol=%s
        LIMIT 1
        """,
        (key, key, key),
    )
    row = cur.fetchone()

    if not row:
        return None

    return {
        "code": row[0],
        "strategy_key": row[1],
        "display_name": row[2],
        "plus500_name": row[3],
        "signals_enabled": bool(row[4]),
        "is_active": bool(row[5]),
    }


def _validate_strategy_rule_for_api(rule: dict) -> dict:
    if not isinstance(rule, dict):
        raise ValueError("Each rule must be object")

    try:
        day_of_week = int(rule.get("day_of_week"))
    except Exception:
        raise ValueError(f"Bad day_of_week={rule.get('day_of_week')!r}")

    if day_of_week < 0 or day_of_week > 6:
        raise ValueError(f"Bad day_of_week={day_of_week}. Allowed 0..6")

    start_time = str(rule.get("start") or rule.get("start_time") or "00:00").strip()
    end_time = str(rule.get("end") or rule.get("end_time") or "24:00").strip()

    _hhmm_to_min_for_strategy(start_time)
    _hhmm_to_min_for_strategy(end_time)

    timeframe = normalize_strategy_timeframe(str(rule.get("timeframe") or ""))
    signal = normalize_strategy_signal(str(rule.get("signal") or ""))

    try:
        skip = int(rule.get("skip", 0))
    except Exception:
        raise ValueError(f"Bad skip={rule.get('skip')!r}")

    if skip < 0:
        raise ValueError("skip must be >= 0")

    trade_amount = rule.get("trade_amount")

    if trade_amount in ("", None):
        trade_amount = None
    else:
        try:
            trade_amount = float(trade_amount)
        except Exception:
            raise ValueError(f"Bad trade_amount={trade_amount!r}")

        if trade_amount <= 0:
            raise ValueError("trade_amount must be > 0")

    return {
        "day_of_week": day_of_week,
        "start": start_time,
        "end": end_time,
        "timeframe": timeframe,
        "signal": signal,
        "skip": skip,
        "trade_amount": trade_amount,
    }


def ensure_strategy_rule_changes_schema() -> None:
    conn = _get_conn()
    if not conn:
        raise RuntimeError("ensure_strategy_rule_changes_schema: нет соединения с БД")

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_rule_changes (
                    id BIGSERIAL PRIMARY KEY,
                    instrument_code TEXT REFERENCES instruments(code) ON DELETE SET NULL,
                    strategy_key TEXT,
                    change_type TEXT NOT NULL,
                    old_rules_json JSONB,
                    new_rules_json JSONB,
                    actor TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS ix_strategy_rule_changes_instrument_created
                ON strategy_rule_changes(instrument_code, created_at DESC);
                """
            )
        conn.commit()
    finally:
        conn.close()


def replace_strategy_week_rules(
    strategy_key: str,
    rules: list[dict],
    *,
    actor: str = "api",
    require_one_rule: bool = True,
) -> dict:
    """
    Полностью заменяет активную недельную таблицу пары.

    Безопасность:
      - старые rules не удаляются физически, а переводятся в is_active=FALSE;
      - новый набор вставляется заново;
      - дубли сигналов разрешены и сохраняются;
      - пустые дни не пишутся в БД = PAUSE;
      - snapshot старых/новых правил пишется в strategy_rule_changes.
    """
    ensure_strategy_rule_changes_schema()

    key = str(strategy_key or "").strip()
    if not key:
        raise ValueError("strategy_key is required")

    if rules is None:
        rules = []

    if not isinstance(rules, list):
        raise ValueError("rules must be list")

    normalized_rules = [_validate_strategy_rule_for_api(r) for r in rules]

    if require_one_rule and not normalized_rules:
        raise ValueError("At least one rule is required")

    conn = _get_conn()
    if not conn:
        raise RuntimeError("replace_strategy_week_rules: нет соединения с БД")

    try:
        with conn.cursor() as cur:
            instr = _resolve_instrument_by_strategy_key(cur, key)
            if not instr:
                raise ValueError(f"Instrument not found by strategy_key/code/symbol: {key}")

            code = instr["code"]
            real_strategy_key = instr["strategy_key"] or key

            old_snapshot = _fetch_strategy_rules_snapshot(cur, code)

            cur.execute(
                """
                UPDATE strategy_rules
                SET is_active=FALSE,
                    updated_at=NOW()
                WHERE instrument_code=%s
                  AND is_active=TRUE
                """,
                (code,),
            )

            order_by_day: dict[int, int] = {}

            inserted_snapshot = []

            for rule in normalized_rules:
                day = int(rule["day_of_week"])
                row_order = order_by_day.get(day, 0)
                order_by_day[day] = row_order + 1

                cur.execute(
                    """
                    INSERT INTO strategy_rules(
                        instrument_code,
                        day_of_week,
                        row_order,
                        start_time,
                        end_time,
                        timeframe,
                        signal,
                        skip_count,
                        trade_amount,
                        is_active
                    )
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE)
                    RETURNING id
                    """,
                    (
                        code,
                        day,
                        row_order,
                        rule["start"],
                        rule["end"],
                        rule["timeframe"],
                        rule["signal"],
                        rule["skip"],
                        rule["trade_amount"],
                    ),
                )

                new_id = int(cur.fetchone()[0])

                inserted_snapshot.append({
                    "id": new_id,
                    "instrument_code": code,
                    "day_of_week": day,
                    "row_order": row_order,
                    "start": rule["start"],
                    "end": rule["end"],
                    "timeframe": rule["timeframe"],
                    "signal": rule["signal"],
                    "is_active": True,
                    "skip": rule["skip"],
                    "trade_amount": rule["trade_amount"],
                })

            cur.execute(
                """
                INSERT INTO strategy_rule_changes(
                    instrument_code,
                    strategy_key,
                    change_type,
                    old_rules_json,
                    new_rules_json,
                    actor
                )
                VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,%s)
                """,
                (
                    code,
                    real_strategy_key,
                    "REPLACE_WEEK",
                    json.dumps(old_snapshot, ensure_ascii=False),
                    json.dumps(inserted_snapshot, ensure_ascii=False),
                    actor,
                ),
            )

            cur.execute(
                """
                UPDATE instruments
                SET is_active=TRUE
                WHERE code=%s
                """,
                (code,),
            )

        conn.commit()

        detail = get_strategy_instrument_detail(real_strategy_key)
        return detail or {"strategy_key": real_strategy_key, "code": code, "week": {}}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def set_strategy_instrument_signals_enabled(
    strategy_key: str,
    enabled: bool,
    *,
    actor: str = "api",
) -> dict:
    ensure_strategy_rule_changes_schema()

    key = str(strategy_key or "").strip()
    if not key:
        raise ValueError("strategy_key is required")

    conn = _get_conn()
    if not conn:
        raise RuntimeError("set_strategy_instrument_signals_enabled: нет соединения с БД")

    try:
        with conn.cursor() as cur:
            instr = _resolve_instrument_by_strategy_key(cur, key)
            if not instr:
                raise ValueError(f"Instrument not found: {key}")

            cur.execute(
                """
                UPDATE instruments
                SET signals_enabled=%s,
                    is_active=TRUE
                WHERE code=%s
                """,
                (bool(enabled), instr["code"]),
            )

            cur.execute(
                """
                INSERT INTO strategy_rule_changes(
                    instrument_code,
                    strategy_key,
                    change_type,
                    old_rules_json,
                    new_rules_json,
                    actor
                )
                VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,%s)
                """,
                (
                    instr["code"],
                    instr["strategy_key"],
                    "SET_SIGNALS_ENABLED",
                    json.dumps({"signals_enabled": instr["signals_enabled"]}, ensure_ascii=False),
                    json.dumps({"signals_enabled": bool(enabled)}, ensure_ascii=False),
                    actor,
                ),
            )

        conn.commit()

        detail = get_strategy_instrument_detail(instr["strategy_key"])
        return detail or {"strategy_key": instr["strategy_key"], "code": instr["code"]}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def soft_delete_strategy_instrument(
    strategy_key: str,
    *,
    actor: str = "api",
) -> dict:
    """
    Soft delete пары:
      - instruments.is_active=FALSE
      - instruments.signals_enabled=FALSE
      - strategy_rules.is_active=FALSE
    Историю instrument_data / instrument_signals / position_state не трогаем.
    """
    ensure_strategy_rule_changes_schema()

    key = str(strategy_key or "").strip()
    if not key:
        raise ValueError("strategy_key is required")

    conn = _get_conn()
    if not conn:
        raise RuntimeError("soft_delete_strategy_instrument: нет соединения с БД")

    try:
        with conn.cursor() as cur:
            instr = _resolve_instrument_by_strategy_key(cur, key)
            if not instr:
                raise ValueError(f"Instrument not found: {key}")

            code = instr["code"]
            real_strategy_key = instr["strategy_key"] or key
            old_snapshot = _fetch_strategy_rules_snapshot(cur, code)

            cur.execute(
                """
                UPDATE strategy_rules
                SET is_active=FALSE,
                    updated_at=NOW()
                WHERE instrument_code=%s
                  AND is_active=TRUE
                """,
                (code,),
            )

            cur.execute(
                """
                UPDATE instruments
                SET is_active=FALSE,
                    signals_enabled=FALSE
                WHERE code=%s
                """,
                (code,),
            )

            cur.execute(
                """
                INSERT INTO strategy_rule_changes(
                    instrument_code,
                    strategy_key,
                    change_type,
                    old_rules_json,
                    new_rules_json,
                    actor
                )
                VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,%s)
                """,
                (
                    code,
                    real_strategy_key,
                    "SOFT_DELETE_INSTRUMENT",
                    json.dumps(old_snapshot, ensure_ascii=False),
                    json.dumps({"is_active": False, "signals_enabled": False}, ensure_ascii=False),
                    actor,
                ),
            )

        conn.commit()

        return {
            "code": code,
            "strategy_key": real_strategy_key,
            "is_active": False,
            "signals_enabled": False,
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()



# ----- Trade amount helpers

def _decimal_to_float_or_none(value):
    if value is None:
        return None
    return float(value)


def _decimal_to_str_or_none(value):
    if value is None:
        return None
    return str(value)


def _trade_amount_to_decimal(value) -> Decimal:
    raw = str(value or "").strip().replace(",", ".")

    if not raw:
        raise ValueError("Количество покупки не может быть пустым.")

    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Некорректное количество покупки: {value!r}") from exc

    if amount <= 0:
        raise ValueError("Количество покупки должно быть больше 0.")

    return amount


def _validate_trade_amount_limits(amount: Decimal, *, min_amount, max_amount, step) -> None:
    if min_amount is not None:
        min_d = Decimal(str(min_amount))
        if amount < min_d:
            raise ValueError(f"Количество покупки меньше минимального Plus500 Unit Amount: {min_d}")

    if max_amount is not None:
        max_d = Decimal(str(max_amount))
        if amount > max_d:
            raise ValueError(f"Количество покупки больше максимального ограничения: {max_d}")

    if step is not None:
        step_d = Decimal(str(step))
        if step_d > 0:
            q = amount / step_d
            if q != q.to_integral_value():
                raise ValueError(f"Количество покупки должно быть кратно шагу Plus500: {step_d}")


def list_instrument_trade_settings() -> list[dict]:
    conn = _get_conn()
    if not conn:
        return []

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    code,
                    strategy_key,
                    trade_amount,
                    trade_amount_currency,
                    trade_amount_unit,
                    trade_amount_updated_at,
                    plus500_min_unit_amount,
                    plus500_amount_step,
                    plus500_max_unit_amount,
                    plus500_initial_margin_pct,
                    plus500_maintenance_margin_pct,
                    plus500_leverage,
                    plus500_restrictions_note,
                    plus500_restrictions_updated_at
                FROM instruments
                ORDER BY COALESCE(strategy_key, code), code
                """
            )
            rows = cur.fetchall()

        out = []

        for row in rows:
            out.append({
                "code": row[0],
                "strategy_key": row[1],
                "trade_amount": _decimal_to_float_or_none(row[2]),
                "trade_amount_raw": _decimal_to_str_or_none(row[2]),
                "trade_amount_currency": row[3],
                "trade_amount_unit": row[4],
                "trade_amount_updated_at": row[5].isoformat(sep=" ") if row[5] else None,
                "plus500_min_unit_amount": _decimal_to_float_or_none(row[6]),
                "plus500_amount_step": _decimal_to_float_or_none(row[7]),
                "plus500_max_unit_amount": _decimal_to_float_or_none(row[8]),
                "plus500_initial_margin_pct": _decimal_to_float_or_none(row[9]),
                "plus500_maintenance_margin_pct": _decimal_to_float_or_none(row[10]),
                "plus500_leverage": row[11],
                "plus500_restrictions_note": row[12],
                "plus500_restrictions_updated_at": row[13].isoformat(sep=" ") if row[13] else None,
            })

        return out

    except Exception:
        logger.exception("Ошибка чтения trade settings")
        return []

    finally:
        conn.close()


def get_instrument_trade_settings(key_or_code: str) -> Optional[dict]:
    needle = str(key_or_code or "").strip()
    if not needle:
        return None

    conn = _get_conn()
    if not conn:
        return None

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    code,
                    strategy_key,
                    trade_amount,
                    trade_amount_currency,
                    trade_amount_unit,
                    trade_amount_updated_at,
                    plus500_min_unit_amount,
                    plus500_amount_step,
                    plus500_max_unit_amount,
                    plus500_initial_margin_pct,
                    plus500_maintenance_margin_pct,
                    plus500_leverage,
                    plus500_restrictions_note,
                    plus500_restrictions_updated_at
                FROM instruments
                WHERE code=%s OR strategy_key=%s OR plus500_symbol=%s
                LIMIT 1
                """,
                (needle, needle, needle),
            )
            row = cur.fetchone()

        if not row:
            return None

        return {
            "code": row[0],
            "strategy_key": row[1],
            "trade_amount": _decimal_to_float_or_none(row[2]),
            "trade_amount_raw": _decimal_to_str_or_none(row[2]),
            "trade_amount_currency": row[3],
            "trade_amount_unit": row[4],
            "trade_amount_updated_at": row[5].isoformat(sep=" ") if row[5] else None,
            "plus500_min_unit_amount": _decimal_to_float_or_none(row[6]),
            "plus500_amount_step": _decimal_to_float_or_none(row[7]),
            "plus500_max_unit_amount": _decimal_to_float_or_none(row[8]),
            "plus500_initial_margin_pct": _decimal_to_float_or_none(row[9]),
            "plus500_maintenance_margin_pct": _decimal_to_float_or_none(row[10]),
            "plus500_leverage": row[11],
            "plus500_restrictions_note": row[12],
            "plus500_restrictions_updated_at": row[13].isoformat(sep=" ") if row[13] else None,
        }

    except Exception:
        logger.exception("Ошибка чтения trade settings key_or_code=%s", needle)
        return None

    finally:
        conn.close()


def set_instrument_trade_amount(
    key_or_code: str,
    trade_amount,
    *,
    actor: str = "api",
) -> dict:
    """
    Устанавливает количество покупки для инструмента.

    Единицы:
      FOREX/CRYPTO: amount_currency = base currency / coin.
      INDEX: trade_amount_unit = CONTRACT.

    Ограничения Plus500:
      - если plus500_min_unit_amount заполнен — amount >= min;
      - если plus500_max_unit_amount заполнен — amount <= max;
      - если plus500_amount_step заполнен — amount кратен step.
    """
    ensure_strategy_rule_changes_schema()

    needle = str(key_or_code or "").strip()
    if not needle:
        raise ValueError("strategy_key/code is required")

    amount = _trade_amount_to_decimal(trade_amount)

    conn = _get_conn()
    if not conn:
        raise RuntimeError("set_instrument_trade_amount: нет соединения с БД")

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    code,
                    strategy_key,
                    amount_unit,
                    amount_currency,
                    price_currency,
                    margin_currency,
                    trade_amount,
                    trade_amount_currency,
                    trade_amount_unit,
                    plus500_min_unit_amount,
                    plus500_amount_step,
                    plus500_max_unit_amount
                FROM instruments
                WHERE code=%s OR strategy_key=%s OR plus500_symbol=%s
                LIMIT 1
                """,
                (needle, needle, needle),
            )
            row = cur.fetchone()

            if not row:
                raise ValueError(f"Instrument not found: {needle}")

            code = row[0]
            strategy_key = row[1]
            amount_unit = row[2]
            amount_currency = row[3] or row[4] or row[5]
            old_trade_amount = row[6]
            old_trade_amount_currency = row[7]
            old_trade_amount_unit = row[8]
            min_amount = row[9]
            step = row[10]
            max_amount = row[11]

            _validate_trade_amount_limits(
                amount,
                min_amount=min_amount,
                max_amount=max_amount,
                step=step,
            )

            cur.execute(
                """
                UPDATE instruments
                SET
                    trade_amount=%s,
                    trade_amount_currency=%s,
                    trade_amount_unit=%s,
                    trade_amount_updated_at=NOW()
                WHERE code=%s
                """,
                (
                    str(amount),
                    amount_currency,
                    amount_unit,
                    code,
                ),
            )

            cur.execute(
                """
                INSERT INTO strategy_rule_changes(
                    instrument_code,
                    strategy_key,
                    change_type,
                    old_rules_json,
                    new_rules_json,
                    actor
                )
                VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,%s)
                """,
                (
                    code,
                    strategy_key,
                    "SET_TRADE_AMOUNT",
                    json.dumps({
                        "trade_amount": _decimal_to_str_or_none(old_trade_amount),
                        "trade_amount_currency": old_trade_amount_currency,
                        "trade_amount_unit": old_trade_amount_unit,
                    }, ensure_ascii=False),
                    json.dumps({
                        "trade_amount": str(amount),
                        "trade_amount_currency": amount_currency,
                        "trade_amount_unit": amount_unit,
                    }, ensure_ascii=False),
                    actor,
                ),
            )

        conn.commit()

        result = get_instrument_trade_settings(strategy_key or code)
        return result or {
            "code": code,
            "strategy_key": strategy_key,
            "trade_amount": float(amount),
            "trade_amount_currency": amount_currency,
            "trade_amount_unit": amount_unit,
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()



def get_strategy_cycle_state(
    instrument_code: str,
    signal_rule: str,
) -> dict:
    conn = _get_conn()
    if not conn:
        return {
            "current_skip": 0,
            "waiting_transition": False,
        }

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    current_skip,
                    waiting_transition
                FROM strategy_cycle_state
                WHERE instrument_code=%s
                  AND signal_rule=%s
                """,
                (
                    instrument_code,
                    signal_rule,
                ),
            )

            row = cur.fetchone()

            if not row:
                return {
                    "current_skip": 0,
                    "waiting_transition": False,
                }

            return {
                "current_skip": int(row[0]),
                "waiting_transition": bool(row[1]),
            }

    except Exception:
        logger.exception(
            "Ошибка get_strategy_cycle_state(%s, %s)",
            instrument_code,
            signal_rule,
        )
        return {
            "current_skip": 0,
            "waiting_transition": False,
        }

    finally:
        conn.close()

def set_strategy_waiting_transition(
    instrument_code: str,
    signal_rule: str,
) -> bool:
    conn = _get_conn()
    if not conn:
        return False

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO strategy_cycle_state(
                    instrument_code,
                    signal_rule,
                    current_skip,
                    waiting_transition
                )
                VALUES(
                    %s,
                    %s,
                    0,
                    TRUE
                )
                ON CONFLICT(instrument_code, signal_rule)
                DO UPDATE
                SET
                    waiting_transition = TRUE,
                    updated_at = NOW()
                """,
                (
                    instrument_code,
                    signal_rule,
                ),
            )

        conn.commit()
        return True

    except Exception:
        logger.exception(
            "Ошибка set_strategy_waiting_transition(%s, %s)",
            instrument_code,
            signal_rule,
        )
        return False

    finally:
        conn.close()

def advance_strategy_cycle(
    instrument_code: str,
    signal_rule: str,
) -> bool:
    conn = _get_conn()
    if not conn:
        return False

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO strategy_cycle_state(
                    instrument_code,
                    signal_rule,
                    current_skip,
                    waiting_transition
                )
                VALUES(
                    %s,
                    %s,
                    1,
                    FALSE
                )
                ON CONFLICT(instrument_code, signal_rule)
                DO UPDATE
                SET
                    current_skip = strategy_cycle_state.current_skip + 1,
                    waiting_transition = FALSE,
                    updated_at = NOW()
                """,
                (
                    instrument_code,
                    signal_rule,
                ),
            )

        conn.commit()
        return True

    except Exception:
        logger.exception(
            "Ошибка advance_strategy_cycle(%s, %s)",
            instrument_code,
            signal_rule,
        )
        return False

    finally:
        conn.close()

def reset_strategy_cycle(
    instrument_code: str,
    signal_rule: str,
) -> bool:
    conn = _get_conn()
    if not conn:
        return False

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO strategy_cycle_state(
                    instrument_code,
                    signal_rule,
                    current_skip,
                    waiting_transition
                )
                VALUES(
                    %s,
                    %s,
                    0,
                    FALSE
                )
                ON CONFLICT(instrument_code, signal_rule)
                DO UPDATE
                SET
                    current_skip = 0,
                    waiting_transition = FALSE,
                    updated_at = NOW()
                """,
                (
                    instrument_code,
                    signal_rule,
                ),
            )

        conn.commit()
        return True

    except Exception:
        logger.exception(
            "Ошибка reset_strategy_cycle(%s, %s)",
            instrument_code,
            signal_rule,
        )
        return False

    finally:
        conn.close()

