"""
Regime Detection and Per-Regime Performance Tracking.

Classifies market regimes from price/volume data:
- TREND: sustained directional move
- MEAN_REVERSION: oscillation around a moving average
- HIGH_VOL / LOW_VOL: volatility quantiles
- PANIC: sharp drawdown with elevated volume

Tracks strategy performance broken down by detected regime.
"""

from __future__ import annotations

import numpy as np
from dataclasses import dataclass
from collections import defaultdict
from typing import Optional

from paper_trading.quant.stats import TradeRecord

# ── Regime Types ────────────────────────────────────────────────────────────────


class RegimeType:
    TREND = "TREND"
    MEAN_REVERSION = "MEAN_REVERSION"
    HIGH_VOL = "HIGH_VOL"
    LOW_VOL = "LOW_VOL"
    PANIC = "PANIC"


@dataclass
class RegimeSnapshot:
    regime: str
    start_bar: int
    end_bar: int
    avg_volatility: float
    directional_score: float  # +1 = strong up, -1 = strong down


# ── Regime Detector ──────────────────────────────────────────────────────────────


class RegimeDetector:
    """
    Classifies market regime using a rolling window of OHLCV data.

    Features used:
    - ADX for trend strength
    - Bollinger bandwidth for volatility
    - Z-score of price relative to SMA for mean-reversion signals
    - Volume ratio vs rolling average for panic detection
    """

    def __init__(
        self,
        lookback: int = 50,
        vol_lookback: int = 20,
        adx_period: int = 14,
        bb_period: int = 20,
        vol_threshold: float = 2.0,  # volume multipler for PANIC
        vol_ma_period: int = 20,
    ):
        self._lookback = lookback
        self._vol_lookback = vol_lookback
        self._adx_period = adx_period
        self._bb_period = bb_period
        self._vol_threshold = vol_threshold
        self._vol_ma_period = vol_ma_period

        self._closes: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._volumes: list[float] = []

    def update(self, candle: dict) -> None:
        """Feed a new candle. Call classify() after update."""
        self._closes.append(candle["close"])
        self._highs.append(candle.get("high", candle["close"]))
        self._lows.append(candle.get("low", candle["close"]))
        self._volumes.append(candle.get("volume", 0.0))

    @property
    def ready(self) -> bool:
        return len(self._closes) >= self._lookback + self._adx_period

    def classify(self, current_bar: Optional[int] = None) -> str:
        """
        Classify the current market regime.
        Must have called update() with enough candles first.
        """
        if not self.ready:
            return RegimeType.LOW_VOL  # Default until enough data

        closes = np.array(self._closes)
        highs = np.array(self._highs)
        lows = np.array(self._lows)
        volumes = np.array(self._volumes)

        # ── 1. ADX for trend strength ───────────────────────────────────────────
        adx = self._compute_adx(highs, lows, closes)
        is_trending = adx > 25

        # ── 2. Volatility percentile (rolling window, adaptive threshold) ──────────
        vol_percentile = self._compute_vol_percentile(closes)

        # ── 3. Mean reversion signal ─────────────────────────────────────────────
        z_score = self._compute_z_score(closes)

        # ── 4. Volume ratio for panic ───────────────────────────────────────────
        vol_ratio = self._compute_volume_ratio(volumes)

        # ── Decision tree ───────────────────────────────────────────────────────
        # PANIC: extreme volume + sharp drop (checked first — overrides everything)
        if vol_ratio > self._vol_threshold and len(closes) >= 2:
            price_change = (closes[-1] - closes[-self._vol_lookback]) / closes[
                -self._vol_lookback
            ]
            if price_change < -0.02:
                return RegimeType.PANIC

        # Trend detection (ADX + z-score) should NOT be overridden by vol check
        # Use z_score for directionality; vol_percentile only for HIGH_VOL / LOW_VOL
        if is_trending and abs(z_score) > 1.0:
            return RegimeType.TREND
        if abs(z_score) < 0.5:
            return RegimeType.MEAN_REVERSION

        # Volatility tiers — only reached if no strong trend/reversion signal
        if vol_percentile > 0.75:
            return RegimeType.HIGH_VOL
        if vol_percentile < 0.25:
            return RegimeType.LOW_VOL

        # Residual: use trend signal if ADX is elevated
        if is_trending:
            return RegimeType.TREND
        return RegimeType.MEAN_REVERSION

    def get_features(self) -> dict:
        """Return raw feature values for logging / debugging."""
        if not self.ready:
            return {}
        closes = np.array(self._closes)
        highs = np.array(self._highs)
        lows = np.array(self._lows)
        volumes = np.array(self._volumes)
        return {
            "adx": round(self._compute_adx(highs, lows, closes), 2),
            "bb_width": round(self._compute_bb_width(closes), 4),
            "z_score": round(self._compute_z_score(closes), 3),
            "volume_ratio": round(self._compute_volume_ratio(volumes), 2),
        }

    # ── Feature computations ───────────────────────────────────────────────────

    def _compute_adx(
        self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray
    ) -> float:
        """Average Directional Index — simplified."""
        period = min(self._adx_period, len(closes) - 1)
        if period < 3:
            return 0.0

        # True range
        tr1 = highs[1:] - lows[1:]
        tr2 = np.abs(highs[1:] - closes[:-1])
        tr3 = np.abs(lows[1:] - closes[:-1])
        tr = np.maximum(tr1, np.maximum(tr2, tr3))

        # Directional movement
        up_move = highs[1:] - highs[:-1]
        down_move = lows[:-1] - lows[1:]
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        # Smoothed
        alpha = 2.0 / (period + 1)
        atr = float(np.mean(tr[-period:]))
        plus_di = float(np.mean(plus_dm[-period:])) / atr if atr > 0 else 0.0
        minus_di = float(np.mean(minus_dm[-period:])) / atr if atr > 0 else 0.0

        di_sum = plus_di + minus_di
        if di_sum == 0:
            return 0.0
        dx = abs(plus_di - minus_di) / di_sum * 100.0
        return float(dx)

    def _compute_bb_width(self, closes: np.ndarray) -> float:
        period = min(self._bb_period, len(closes) - 1)
        if period < 2:
            return 0.0
        band = closes[-period:]
        mid = np.mean(band)
        std = np.std(band)
        if mid == 0:
            return 0.0
        return float(std / mid)

    def _compute_vol_percentile(self, closes: np.ndarray) -> float:
        """
        Compute how volatile current window is relative to a rolling history.
        Returns 0.0 (calmest) to 1.0 (most volatile) — used for LOW_VOL/HIGH_VOL tiers.
        """
        if len(closes) < self._vol_lookback * 3:
            # Not enough history for percentile — be conservative
            return 0.5

        # Use log-returns std as volatility proxy over the lookback window
        recent_closes = closes[-self._vol_lookback :]
        returns = np.diff(np.log(recent_closes + 1e-10))
        current_vol = float(np.std(returns))

        # Build rolling volatility history with half-overlapping windows
        n = len(closes)
        vol_history = []
        step = self._vol_lookback // 2
        for i in range(self._vol_lookback, n, step):
            window = closes[i - self._vol_lookback : i]
            if len(window) < self._vol_lookback - 1:
                continue
            rets = np.diff(np.log(window + 1e-10))
            vol_history.append(float(np.std(rets)))

        if not vol_history:
            return 0.5

        vol_arr = np.array(vol_history)
        count_below = int(np.sum(vol_arr < current_vol))
        return float(count_below / len(vol_arr))

    def _compute_z_score(self, closes: np.ndarray) -> float:
        period = min(self._lookback, len(closes) - 1)
        if period < 5:
            return 0.0
        band = closes[-period:]
        mean = np.mean(band)
        std = np.std(band)
        if std == 0:
            return 0.0
        return float((closes[-1] - mean) / std)

    def _compute_volume_ratio(self, volumes: np.ndarray) -> float:
        period = min(self._vol_ma_period, len(volumes) - 1)
        if period < 2:
            return 1.0
        recent = np.mean(volumes[-period:])
        hist = np.mean(volumes[:-period])
        return float(recent / hist) if hist > 0 else 1.0


# ── Regime Performance Tracker ─────────────────────────────────────────────────


class RegimePerformanceTracker:
    """
    Collects per-regime performance data as a backtest runs.
    At the end of a run, call get_summary() to get regime breakdown.
    """

    def __init__(self):
        self._regime_trades: dict[str, list[TradeRecord]] = defaultdict(list)
        self._current_regime: str = RegimeType.LOW_VOL
        self._regime_spans: list[RegimeSnapshot] = []

    def set_regime(self, regime: str, bar: int) -> None:
        self._current_regime = regime

    def record_trade(self, trade: TradeRecord) -> None:
        trade.regime = self._current_regime
        self._regime_trades[self._current_regime].append(trade)

    def get_summary(self) -> dict:
        """Return per-regime stats."""
        summary = {}
        for regime, trades in self._regime_trades.items():
            if not trades:
                continue
            wins = [t for t in trades if t.pnl > 0]
            losses = [t for t in trades if t.pnl <= 0]
            n = len(trades)
            total_pnl = sum(t.pnl for t in trades)
            summary[regime] = {
                "n_trades": n,
                "win_rate": round(len(wins) / n, 4),
                "total_pnl": round(total_pnl, 2),
                "avg_pnl": round(total_pnl / n, 4),
                "profit_factor": (
                    round(sum(t.pnl for t in wins) / abs(sum(t.pnl for t in losses)))
                    if losses
                    else float("inf")
                ),
                "best_trade": round(max(t.pnl for t in trades), 4),
                "worst_trade": round(min(t.pnl for t in trades), 4),
            }
        return summary

    def reset(self) -> None:
        self._regime_trades.clear()
        self._regime_spans.clear()
        self._current_regime = RegimeType.LOW_VOL
