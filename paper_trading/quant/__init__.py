"""
Quantitative Research Framework.

Submodules:
    interface   — Strategy contract, Signal API, Parameter schema
    research   — ResearchEnvironment, backtest engine, parameter sweeps
    stats      — PerformanceStats, EquityCurve, MonteCarloEngine
    regime     — RegimeDetector, RegimePerformanceTracker
    anti_overfit — AntiOverfitEngine, walk-forward, bootstrap stability
    portfolio  — PortfolioBuilder, correlation matrix

Usage:
    from paper_trading.quant import ResearchEnvironment, BaseStrategy, Parameter, SignalType

    class RSIStrategy(BaseStrategy):
        def parameters(self):
            return [
                Parameter("period", 14, min=5, max=50),
                Parameter("oversold", 30, min=20, max=40),
            ]
        def on_bar(self, candle):
            rsi = self._compute_rsi(candle["close"])
            if rsi < self.params["oversold"]:
                return Signal(SignalType.BUY, self.symbol)
            return None

    env = ResearchEnvironment(candles=my_candles, exchange=exchange)
    result = env.scan(RSIStrategy, {"period": [7, 14, 21], "oversold": [25, 30, 35]})
    print(result.ranking[0].stats)
"""

from paper_trading.quant.interface import (
    BaseStrategy,
    Parameter,
    Signal,
    SignalType,
    sma,
    ema,
    rsi,
    atr,
    bollinger_bands,
    momentum,
)
from paper_trading.quant.research import ResearchEnvironment, SweepResult, ParameterSet
from paper_trading.quant.stats import (
    PerformanceStats,
    EquityCurve,
    TradeRecord,
    StatsCalculator,
    MonteCarloEngine,
    MonteCarloResult,
)
from paper_trading.quant.regime import (
    RegimeType,
    RegimeDetector,
    RegimePerformanceTracker,
)
from paper_trading.quant.anti_overfit import (
    AntiOverfitEngine,
    StabilityReport,
    bootstrap_stability,
)
from paper_trading.quant.portfolio import (
    PortfolioBuilder,
    PortfolioConfig,
    PortfolioResult,
    build_correlation_matrix,
)

__all__ = [
    # Interface
    "BaseStrategy",
    "Parameter",
    "Signal",
    "SignalType",
    "sma",
    "ema",
    "rsi",
    "atr",
    "bollinger_bands",
    "momentum",
    # Research
    "ResearchEnvironment",
    "SweepResult",
    "ParameterSet",
    # Stats
    "PerformanceStats",
    "EquityCurve",
    "TradeRecord",
    "StatsCalculator",
    "MonteCarloEngine",
    "MonteCarloResult",
    # Regime
    "RegimeType",
    "RegimeDetector",
    "RegimePerformanceTracker",
    # Anti-overfit
    "AntiOverfitEngine",
    "StabilityReport",
    "bootstrap_stability",
    # Portfolio
    "PortfolioBuilder",
    "PortfolioConfig",
    "PortfolioResult",
    "build_correlation_matrix",
]
