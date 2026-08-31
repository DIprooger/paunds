from dataclasses import dataclass
from typing import Dict, Any, Optional
from .config import InstrumentConfig

@dataclass
class InstrumentResult:
    instrument: InstrumentConfig
    price_display: Optional[str]
    price_number: Optional[float]
    timeframes: Dict[str, Dict[str, str]]
    technical_indicators: Dict[str, Any]
    pivot_points: Dict[str, Any]
    executed_at: str
