"""
Multi-Strategy Portfolio Layer.

Provides:
- Portfolio allocation across multiple strategies
- Correlation-based rebalancing
- Exposure and risk budgeting
- Strategy ranking and selection
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from paper_trading.quant.stats import PerformanceStats


@dataclass
class StrategyAllocation:
    """A single strategy's allocation within the portfolio."""

    strategy_id: str
    strategy_name: str
    weight: float  # 0.0–1.0
    sharpe: float = 0.0
    correlation: float = 0.0  # correlation to portfolio
    exposure: float = 0.0  # notional exposure
    annual_return: float = 0.0
    max_drawdown: float = 0.0


@dataclass
class PortfolioConfig:
    """Configuration for the portfolio layer."""

    max_strategies: int = 5
    min_weight: float = 0.05  # minimum allocation per strategy
    max_weight: float = 0.60  # maximum allocation per strategy
    correlation_threshold: float = 0.75  # max correlation between any two strategies
    target_portfolio_sharpe: float = 1.0  # for Kelly-based sizing


@dataclass
class PortfolioResult:
    """Output from portfolio construction."""

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    allocations: list[StrategyAllocation] = field(default_factory=list)
    portfolio_sharpe: float = 0.0
    portfolio_return: float = 0.0
    portfolio_volatility: float = 0.0
    portfolio_max_dd: float = 0.0
    diversification_ratio: float = 0.0  # weighted avg correlation benefit
    selected_strategies: list[str] = field(default_factory=list)
    rejected_strategies: list[str] = field(default_factory=list)


class PortfolioBuilder:
    """
    Constructs a multi-strategy portfolio using:
    1. Strategy ranking by Sharpe + Sortino
    2. Correlation filtering to avoid concentration
    3. Risk-parity-inspired weighting

    Usage:
        builder = PortfolioBuilder(config)
        result = builder.allocate(strategy_stats_list)
    """

    def __init__(self, config: Optional[PortfolioConfig] = None):
        self._cfg = config or PortfolioConfig()

    def allocate(self, strategy_stats: list[PerformanceStats]) -> PortfolioResult:
        """
        Build optimal portfolio from a list of strategy performance stats.

        Process:
        1. Rank strategies by composite score (Sharpe 60% + Sortino 40%)
        2. Filter correlated strategies (> threshold → keep higher-ranked)
        3. Weight remaining strategies by risk-parity (inverse vol weighting)
        4. Apply min/max weight constraints
        """
        run_id = uuid.uuid4().hex[:8]

        if not strategy_stats:
            return PortfolioResult(run_id=run_id)

        # ── 1. Score and rank ───────────────────────────────────────────────────
        scored = []
        for s in strategy_stats:
            score = 0.6 * s.sharpe_ratio + 0.4 * s.sortino_ratio
            scored.append((score, s))

        scored.sort(key=lambda x: x[0], reverse=True)
        ranked = [s for _, s in scored]

        # ── 2. Correlation filtering ────────────────────────────────────────────
        # Use Sharpe as proxy for returns; correlation computed from regime breakdown
        selected = self._correlation_filter(ranked)

        # ── 3. Weight by inverse volatility (risk parity) ───────────────────────
        total_inv_vol = 0.0
        for s in selected:
            vol = max(s.annualized_volatility, 1e-6)
            total_inv_vol += 1.0 / vol

        raw_weights = {}
        for s in selected:
            vol = max(s.annualized_volatility, 1e-6)
            raw_weights[s.run_id] = (1.0 / vol) / total_inv_vol

        # ── 4. Apply min/max constraints ────────────────────────────────────────
        allocations = []
        excess = 0.0
        constrained_weights = {}

        for sid, w in raw_weights.items():
            if w < self._cfg.min_weight:
                excess += w
                constrained_weights[sid] = self._cfg.min_weight
            elif w > self._cfg.max_weight:
                excess += w - self._cfg.max_weight
                constrained_weights[sid] = self._cfg.max_weight
            else:
                constrained_weights[sid] = w

        # Redistribute excess proportionally among mid-range strategies
        n_mid = sum(
            1
            for w in constrained_weights.values()
            if self._cfg.min_weight <= w <= self._cfg.max_weight
        )
        if n_mid > 0 and excess > 0:
            per_strat = excess / n_mid
            for sid in list(constrained_weights):
                if (
                    self._cfg.min_weight
                    <= constrained_weights[sid]
                    <= self._cfg.max_weight
                ):
                    constrained_weights[sid] = min(
                        constrained_weights[sid] + per_strat, self._cfg.max_weight
                    )

        # ── 5. Build allocations ─────────────────────────────────────────────────
        selected_ids = set()
        for s in strategy_stats:
            if s.run_id in constrained_weights:
                w = constrained_weights[s.run_id]
                alloc = StrategyAllocation(
                    strategy_id=s.run_id,
                    strategy_name=s.strategy_name,
                    weight=round(w, 4),
                    sharpe=s.sharpe_ratio,
                    annual_return=s.annualized_return,
                    max_drawdown=s.max_drawdown_pct,
                )
                allocations.append(alloc)
                selected_ids.add(s.run_id)

        # ── 6. Portfolio-level stats ────────────────────────────────────────────
        weights_arr = np.array([a.weight for a in allocations])
        sharpes = np.array([a.sharpe for a in allocations])
        rets = np.array([a.annual_return for a in allocations])
        dds = np.array([a.max_drawdown for a in allocations])
        vols = np.array(
            [
                getattr(s, "annualized_volatility", 0.1)
                for s in strategy_stats
                if s.run_id in constrained_weights
            ]
        )

        # Weighted average portfolio metrics
        port_sharpe = float(np.dot(weights_arr, sharpes))
        port_return = float(np.dot(weights_arr, rets))
        # Portfolio vol via weighted correlation (simplified — assume zero corr for now)
        port_vol = float(np.sqrt(np.dot(weights_arr**2, vols**2)))
        port_dd = float(np.dot(weights_arr, dds))

        rejected = [s.run_id for s in strategy_stats if s.run_id not in selected_ids]

        return PortfolioResult(
            run_id=run_id,
            allocations=sorted(allocations, key=lambda a: a.weight, reverse=True),
            portfolio_sharpe=round(port_sharpe, 3),
            portfolio_return=round(port_return, 4),
            portfolio_volatility=round(port_vol, 4),
            portfolio_max_dd=round(port_dd, 4),
            diversification_ratio=round(
                1.0 - float(np.mean([a.correlation for a in allocations])), 3
            ),
            selected_strategies=[s.strategy_name for s in allocations],
            rejected_strategies=[
                s.strategy_name for s in strategy_stats if s.run_id in rejected
            ],
        )

    def _correlation_filter(
        self, ranked_strategies: list[PerformanceStats]
    ) -> list[PerformanceStats]:
        """
        Greedy correlation filtering:
        Keep highest-ranked strategy, then skip any strategy with
        correlation > threshold to already-kept strategies.
        Uses regime win-rate vectors as the correlation basis.
        """
        selected = []
        for s in ranked_strategies:
            if len(selected) >= self._cfg.max_strategies:
                break
            # Compute correlation to already-selected strategies
            is_too_correlated = False
            for kept in selected:
                corr = self._regime_correlation(s, kept)
                if corr > self._cfg.correlation_threshold:
                    is_too_correlated = True
                    break
            if not is_too_correlated:
                selected.append(s)
        return selected

    @staticmethod
    def _regime_correlation(s1: PerformanceStats, s2: PerformanceStats) -> float:
        """
        Correlation between two strategies based on per-regime win rates.
        Returns Pearson correlation of their regime win-rate vectors.
        """
        r1 = s1.regime_stats or {}
        r2 = s2.regime_stats or {}
        all_regimes = set(r1.keys()) | set(r2.keys())
        if not all_regimes:
            return 0.0

        vec1 = np.array(
            [r1.get(r, {}).get("win_rate", 0.5) for r in sorted(all_regimes)]
        )
        vec2 = np.array(
            [r2.get(r, {}).get("win_rate", 0.5) for r in sorted(all_regimes)]
        )

        if np.std(vec1) < 1e-9 or np.std(vec2) < 1e-9:
            return 0.0
        corr = np.corrcoef(vec1, vec2)
        if corr.shape == (2, 2):
            return float(corr[0, 1])
        return 0.0


# ── Correlation Matrix Builder ──────────────────────────────────────────────────


def build_correlation_matrix(
    strategy_stats: list[PerformanceStats],
) -> tuple[list[str], np.ndarray]:
    """
    Build a Pearson correlation matrix from strategy returns (via regime win-rate proxy).
    Returns (strategy_names, correlation_matrix).
    """
    if not strategy_stats:
        return [], np.array([])

    names = [s.strategy_name for s in strategy_stats]
    n = len(strategy_stats)

    # Build return vectors per strategy (proxy: use per-regime avg pnl as return proxy)
    regime_map = {}
    for s in strategy_stats:
        for regime, data in (s.regime_stats or {}).items():
            regime_map.setdefault(regime, {})[s.run_id] = data.get("total_pnl", 0.0)

    if not regime_map:
        return names, np.eye(n)

    regimes = sorted(regime_map.keys())
    matrix = np.zeros((n, n))
    for i, si in enumerate(strategy_stats):
        for j, sj in enumerate(strategy_stats):
            vec_i = np.array([regime_map[r].get(si.run_id, 0.0) for r in regimes])
            vec_j = np.array([regime_map[r].get(sj.run_id, 0.0) for r in regimes])
            if np.std(vec_i) < 1e-9 or np.std(vec_j) < 1e-9:
                matrix[i, j] = 0.0 if i != j else 1.0
            else:
                c = np.corrcoef(vec_i, vec_j)
                matrix[i, j] = (
                    float(c[0, 1]) if c.shape == (2, 2) else (1.0 if i == j else 0.0)
                )

    return names, matrix
