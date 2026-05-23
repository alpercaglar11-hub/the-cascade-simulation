"""
Quantitative Research Framework — Strategy Interface.

Defines the pluggable strategy contract and standardized signal API.
All strategies inherit from BaseStrategy and emit Signals.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Optional

import numpy as np

# ── Signal Types ────────────────────────────────────────────────────────────────


class SignalType(Enum):
    BUY = auto()
    SELL = auto()
    HOLD = auto()
    CLOSE_LONG = auto()
    CLOSE_SHORT = auto()


@dataclass
class Signal:
    """
    Standardized output from any strategy.

    Attributes:
        type: BUY / SELL / HOLD / CLOSE_LONG / CLOSE_SHORT
        symbol: e.g. "BTC/USDT"
        strength: 0.0–1.0 confidence (used by portfolio layer for sizing)
        price: price at which signal was generated
        stop_loss: optional SL price
        take_profit: optional TP price
        metadata: free-form dict for strategy-specific data (RSI, position, etc.)
        timestamp: when the signal was generated
        id: unique signal identifier
    """

    type: SignalType
    symbol: str
    strength: float = 1.0
    price: float = 0.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    metadata: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.utcnow())
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def is_actionable(self) -> bool:
        """Return True if this signal requires an order placement."""
        return self.type in (
            SignalType.BUY,
            SignalType.SELL,
            SignalType.CLOSE_LONG,
            SignalType.CLOSE_SHORT,
        )


# ── Parameter Schema ────────────────────────────────────────────────────────────


@dataclass
class Parameter:
    """
    A typed strategy parameter with optional bounds.
    The research framework uses the schema for:
    - Injection during backtesting
    - Grid/random sweep construction
    - Stability validation (param_perturbed_std)
    """

    name: str
    default: Any
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None  # for grid sweeps
    description: str = ""

    def __post_init__(self):
        if isinstance(self.default, bool):
            self.dtype = bool
        elif isinstance(self.default, int):
            self.dtype = int
        elif isinstance(self.default, float):
            self.dtype = float
        elif isinstance(self.default, str):
            self.dtype = str
        else:
            self.dtype = type(self.default)

    def validate(self, value: Any) -> Any:
        """Clamp and cast value to the parameter's type."""
        if self.min is not None and value < self.min:
            value = self.min
        if self.max is not None and value > self.max:
            value = self.max
        return self.dtype(value)


# ── Strategy Interface ─────────────────────────────────────────────────────────


class BaseStrategy(ABC):
    """
    Abstract base for all trading strategies.

    Subclasses must implement:
        - parameters()  -> list[Parameter]
        - on_bar(candle: dict) -> Optional[Signal]

    Optional overrides:
        - on_init()           called once at strategy initialization
        - on_reset()          called on backtest reset
        - get_state()          for serialization
        - load_state(state)    for checkpoint restore

    Usage:
        class RSIStrategy(BaseStrategy):
            def parameters(self):
                return [
                    Parameter("period", 14, min=5, max=50, step=1),
                    Parameter("oversold", 30, min=10, max=40),
                    Parameter("overbought", 70, min=60, max=90),
                ]

            def on_bar(self, candle):
                rsi = self._compute_rsi(candle["close"])
                if rsi < self.params.oversold:
                    return Signal(SignalType.BUY, self.symbol, strength=abs(rsi - 30)/30)
                ...
    """

    # Set by ResearchEnvironment at registration
    symbol: str = "BTC/USDT"

    def __init__(self, params: Optional[dict] = None):
        self.params = self._bind_params(params or {})
        self._state: dict = {}
        self._initialized = False

    # ── Parameter binding ──────────────────────────────────────────────────────

    def _bind_params(self, overrides: dict) -> dict:
        """Merge defaults with overrides, validating bounds."""
        schema = {p.name: p for p in self.parameters()}
        bound = {}
        for name, param in schema.items():
            value = overrides.get(name, param.default)
            bound[name] = param.validate(value)
        return bound

    def inject(self, **kwargs) -> "BaseStrategy":
        """
        Return a new strategy instance with parameter overrides injected.
        Used by the research layer for parameter sweeps.
        """
        merged = {**self.params, **kwargs}
        return self.__class__(params=merged)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @abstractmethod
    def parameters(self) -> list[Parameter]:
        """Return the strategy's parameter schema."""
        ...

    def on_init(self) -> None:
        """Override to set up indicators, load state, etc."""
        self._initialized = True

    def on_reset(self) -> None:
        """Override to reset per-backtest state."""
        self._state.clear()

    @abstractmethod
    def on_bar(self, candle: dict) -> Optional[Signal]:
        """
        Main signal generation hook. Called once per candlestick.

        Args:
            candle: dict with keys: timestamp, open, high, low, close, volume

        Returns:
            Signal or None (no signal = HOLD)
        """
        ...

    # ── State helpers ──────────────────────────────────────────────────────────

    def get_state(self) -> dict:
        """Subclasses can override to save indicator state."""
        return {"params": self.params, "_state": self._state}

    def load_state(self, state: dict) -> None:
        self.params = state.get("params", self.params)
        self._state = state.get("_state", {})

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.params})"


# ── Common Indicator Helpers ────────────────────────────────────────────────────


def sma(closes: np.ndarray, period: int) -> float:
    if len(closes) < period:
        return np.nan
    return float(np.mean(closes[-period:]))


def ema(closes: np.ndarray, period: int) -> float:
    if len(closes) < period:
        return np.nan
    return float(np.convolve(closes, np.ones(period) / period, mode="valid")[-1])


def rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def atr(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14
) -> float:
    if len(highs) < period + 1:
        return np.nan
    high_low = highs[1:] - lows[1:]
    high_close = np.abs(highs[1:] - closes[:-1])
    low_close = np.abs(lows[1:] - closes[:-1])
    tr = np.maximum(high_low, np.maximum(high_close, low_close))
    return float(np.mean(tr[-period:]))


def bollinger_bands(
    closes: np.ndarray, period: int = 20, std_dev: float = 2.0
) -> tuple[float, float, float]:
    if len(closes) < period:
        return np.nan, np.nan, np.nan
    band = closes[-period:]
    mid = float(np.mean(band))
    std = float(np.std(band))
    return mid - std_dev * std, mid, mid + std_dev * std


def momentum(closes: np.ndarray, period: int = 10) -> float:
    if len(closes) < period + 1:
        return 0.0
    return float(closes[-1] / closes[-period] - 1.0)
