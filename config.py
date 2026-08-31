# config.py
import os
from dataclasses import dataclass
from typing import List, Optional

from dotenv import load_dotenv
load_dotenv()

INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "0"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_DELAY_SECONDS = int(os.getenv("RETRY_DELAY_SECONDS", "3"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "pounds.log")

FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
FIRECRAWL_URL = os.getenv("FIRECRAWL_URL", "https://api.firecrawl.dev/v1/scrape")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "/app/credentials.json")
SHEET_NAME_BOT = os.getenv("SHEET_NAME_BOT", "Бот")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_IDS", "").split(",") if cid.strip()]
TELEGRAM_ERROR_CHAT_ID = os.getenv("TELEGRAM_ERROR_CHAT_ID", "").strip()

PLUS500_OPEN_URL = os.getenv("PLUS500_OPEN_URL")
PLUS500_CLOSE_POSITION_URL = os.getenv("PLUS500_CLOSE_POSITION_URL")
PLUS500_PREPARE_URL = os.getenv("PLUS500_PREPARE_URL")
PLUS500_CLOSE_URL = os.getenv("PLUS500_CLOSE_URL")
PLUS500_CLOSE_PAGE_URL = os.getenv("PLUS500_CLOSE_PAGE_URL")
if not PLUS500_CLOSE_PAGE_URL:
    if PLUS500_CLOSE_URL and PLUS500_CLOSE_URL.rstrip("/").endswith("/close"):
        PLUS500_CLOSE_PAGE_URL = PLUS500_CLOSE_URL.rstrip("/") + "_page"
    else:
        PLUS500_CLOSE_PAGE_URL = PLUS500_CLOSE_URL
PLUS500_SIGNAL_SECRET = os.getenv("PLUS500_SIGNAL_SECRET")

TIMEZONE = os.getenv("TIMEZONE", "Europe/Ljubljana")

SHEET_NAME_GBPUSD = os.getenv("SHEET_NAME_GBPUSD", "DATA FOR GBPUSD")
SHEET_NAME_EURGBP = os.getenv("SHEET_NAME_EURGBP", "DATA FOR EURGBP")
SHEET_NAME_AUDUSD = os.getenv("SHEET_NAME_AUDUSD", "DATA FOR AUDUSD")
SHEET_NAME_EURUSD = os.getenv("SHEET_NAME_EURUSD", "DATA FOR EURUSD")
SHEET_NAME_USDJPY = os.getenv("SHEET_NAME_USDJPY", "DATA FOR USDJPY")
SHEET_NAME_USDCAD = os.getenv("SHEET_NAME_USDCAD", "DATA FOR USDCAD")
SHEET_NAME_SPX = os.getenv("SHEET_NAME_SPX", "DATA FOR S&P")
SHEET_NAME_BITCOIN = os.getenv("SHEET_NAME_BITCOIN", "DATA FOR BITCOIN")
SHEET_NAME_SOLBTC = os.getenv("SHEET_NAME_SOLBTC", "DATA FOR SOLBTC")
SHEET_NAME_EURCHF = os.getenv("SHEET_NAME_EURCHF", "DATA FOR EUR/CHF")
SHEET_NAME_AUDNZD = os.getenv("SHEET_NAME_AUDNZD", "DATA FOR AUD/NZD")
SHEET_NAME_CHFJPY = os.getenv("SHEET_NAME_CHFJPY", "DATA FOR CHF/JPY")

def env_bool(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip().lower()
    return v in ("1", "true", "yes", "y", "on")

SHEETS_WRITE_ENABLED = env_bool("SHEETS_WRITE_ENABLED", True)
SIM_FIRECRAWL_REQUESTS = env_bool("SIM_FIRECRAWL_REQUESTS", True)

@dataclass
class InstrumentConfig:
    code: str                 # GBPUSD / EURGBP / ...
    name: str                 # как показывать в логах/Sheets
    url: str
    sheet_name: str
    plus500_name: Optional[str] = None   # "EUR/GBP"
    signals_enabled: bool = False        # отправлять ли сигналы


INSTRUMENTS: List[InstrumentConfig] = [
    InstrumentConfig(
        code="GBPUSD",
        name=os.getenv("GBPUSD_NAME", "GBPUSD"),
        url=os.getenv("GBPUSD_URL", "https://www.investing.com/currencies/gbp-usd-technical"),
        sheet_name=SHEET_NAME_GBPUSD,
        plus500_name=os.getenv("GBPUSD_PLUS500_NAME", "GBP/USD"),
        signals_enabled=True,
    ),
    InstrumentConfig(
        code="EURGBP",
        name=os.getenv("EURGBP_NAME", "EURGBP"),
        url=os.getenv("EURGBP_URL", "https://www.investing.com/currencies/eur-gbp"),
        sheet_name=SHEET_NAME_EURGBP,
        plus500_name=os.getenv("EURGBP_PLUS500_NAME", "EUR/GBP"),
        signals_enabled=True,
    ),
    InstrumentConfig(
        code="AUDUSD",
        name=os.getenv("AUDUSD_NAME", "AUDUSD"),
        url=os.getenv("AUDUSD_URL", "https://www.investing.com/currencies/aud-usd"),
        sheet_name=SHEET_NAME_AUDUSD,
        signals_enabled=True,
    ),
    InstrumentConfig(
        code="EURUSD",
        name=os.getenv("EURUSD_NAME", "EURUSD"),
        url=os.getenv("EURUSD_URL", "https://www.investing.com/currencies/eur-usd"),
        sheet_name=SHEET_NAME_EURUSD,
        plus500_name=os.getenv("EURUSD_PLUS500_NAME", "EUR/USD"),
        signals_enabled=True,
    ),
    InstrumentConfig(
        code="USDJPY",
        name=os.getenv("USDJPY_NAME", "USDJPY"),
        url=os.getenv("USDJPY_URL", "https://www.investing.com/currencies/usd-jpy"),
        sheet_name=SHEET_NAME_USDJPY,
        plus500_name=os.getenv("USDJPY_PLUS500_NAME", "USD/JPY"),
        signals_enabled=True,
    ),
    InstrumentConfig(
        code="USDCAD",
        name=os.getenv("USDCAD_NAME", "USDCAD"),
        url=os.getenv("USDCAD_URL", "https://www.investing.com/currencies/usd-cad"),
        sheet_name=SHEET_NAME_USDCAD,
        signals_enabled=True,
    ),
    InstrumentConfig(
        code="S&P500",
        name=os.getenv("SP500_FUT_NAME", "US_SPX_500_FUT"),
        url=os.getenv("SP500_FUT_URL", "https://www.investing.com/indices/us-spx-500-futures?cid=1175153"),
        sheet_name=SHEET_NAME_SPX,
        plus500_name=os.getenv("S&P500_PLUS500_NAME", "S&P 500"),
        signals_enabled=True,
    ),
    InstrumentConfig(
        code="BITCOIN",
        name=os.getenv("BITCOIN_NAME", "BITCOIN_RU"),
        url=os.getenv("BITCOIN_URL", "https://ru.investing.com/crypto/bitcoin"),
        sheet_name=SHEET_NAME_BITCOIN,
        signals_enabled=False,
    ),
    InstrumentConfig(
        code="SOLBTC",
        name=os.getenv("SOLBTC_NAME", "SOLBTC"),
        url=os.getenv("SOLBTC_URL", "https://www.investing.com/crypto/solana/sol-btc-technical"),
        sheet_name=SHEET_NAME_SOLBTC,
        signals_enabled=False,
        ),
    InstrumentConfig(
        code="EURCHF",
        name=os.getenv("EURCHF_NAME", "EURCHF"),
        url=os.getenv("EURCHF_URL", "https://www.investing.com/currencies/eur-chf"),
        sheet_name=SHEET_NAME_EURCHF,
        plus500_name=os.getenv("EURCHF_PLUS500_NAME", "EUR/CHF"),
        signals_enabled=True,  # поставьте True, если нужно отправлять сигналы
    ),
    InstrumentConfig(
        code="AUDNZD",
        name=os.getenv("AUDNZD_NAME", "AUDNZD"),
        url=os.getenv("AUDNZD_URL", "https://www.investing.com/currencies/aud-nzd"),
        sheet_name=SHEET_NAME_AUDNZD,
        plus500_name=os.getenv("AUDNZD_PLUS500_NAME", "AUD/NZD"),
        signals_enabled=True,
    ),
    InstrumentConfig(
        code="CHFJPY",
        name=os.getenv("CHFJPY_NAME", "CHFJPY"),
        url=os.getenv("CHFJPY_URL", "https://www.investing.com/currencies/chf-jpy"),
        sheet_name=SHEET_NAME_CHFJPY,
        plus500_name=os.getenv("CHFJPY_PLUS500_NAME", "CHF/JPY"),
        signals_enabled=True,
    ),

]
