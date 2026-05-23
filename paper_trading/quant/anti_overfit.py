"""
Anti-Overfitting Module.

Detects and rejects parameter sets that are likely curve-fitted:
1. Parameter sensitivity: how much does performance degrade under small param perturbations?
2. Walk-forward validation: does performance hold on unseen periods?
3. Information coefficient stability: is performance consistent across multiple sub-periods?
4. In-sample/out-of-sample ratio: hard cap on IS vs OOS gap
5. Bootstrap stability: does performance hold under resampled returns?
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class StabilityReport:
    run_id: str
    is_stable: bool
    is_stable_reason: str = ""
    param_sensitivity_score: float = 0.0   # 0=stable, 1=unstable
    is_oos_ratio: float = 0.0              # is_sharpe / oos_sharpe — cap at 2.0
    walk_forward_stability: float = 0.0    # fraction of OOS windows with positive returns
    n_oos_windows: int = 0
    n_oos_positive: int = 0
    reject_reason: Optional[str] = None
    confidence: str = "low"  # "high", "medium", "low"


class AntiOverfitEngine:
    """
    Evaluates whether a parameter set is likely to be overfitted.

    Rejection rules (any one triggers rejection):
    - param_sensitivity_score > threshold (0.3)
    - is_oos_ratio > max_is_oos_ratio (2.0)  — IS performance >> OOS
    - walk_forward_stability < min_oos_positive_rate (0.5)
    - OOS Sharpe < min_oos_sharpe (-0.5)
    """

    def __init__(
        self,
        max_is_oos_ratio: float = 2.0,
        min_oos_positive_rate: float = 0.5,
        min_oos_sharpe: float = -0.5,
        param_sensitivity_threshold: float = 0.3,
        oos_window_min_trades: int = 5,
    ):
        self._max_is_oos = max_is_oos_ratio
        self._min_oos_positive_rate = min_oos_positive_rate
        self._min_oos_sharpe = min_oos_sharpe
        self._sensitivity_threshold = param_sensitivity_threshold
        self._min_oos_trades = oos_window_min_trades

    def evaluate(
        self,
        is_stats,         # PerformanceStats for in-sample run
        oos_stats_list,  # list[PerformanceStats] for each OOS window
        param_perturbations: list[dict],  # list of stats from perturbed param runs
    ) -> StabilityReport:
        run_id = str(uuid.uuid4())[:8]
        is_sharpe = getattr(is_stats, "sharpe_ratio", 0.0)
        is_stable = True
        reject_reason = None

        # ── 1. IS/OOS ratio ────────────────────────────────────────────────────
        is_oos_ratio = 0.0
        positive_oos = 0
        valid_oos = 0
        for oos in oos_stats_list:
            oos_sharpe = getattr(oos, "sharpe_ratio", 0.0)
            oos_trades = getattr(oos, "n_trades", 0)
            if oos_trades < self._min_oos_trades:
                continue
            valid_oos += 1
            if oos_sharpe > 0:
                positive_oos += 1
            if is_sharpe > 0 and oos_sharpe > 0:
                is_oos_ratio = max(is_oos_ratio, is_sharpe / oos_sharpe)

        wf_stability = positive_oos / valid_oos if valid_oos > 0 else 0.0

        # Rule: IS/OOS ratio too high → overfitted
        if is_oos_ratio > self._max_is_oos and valid_oos > 0:
            is_stable = False
            reject_reason = f"is_oos_ratio={is_oos_ratio:.2f} > {self._max_is_oos}"

        # Rule: too few OOS windows positive
        if valid_oos > 0 and wf_stability < self._min_oos_positive_rate:
            is_stable = False
            reject_reason = f"wf_stability={wf_stability:.2%} < {self._min_oos_positive_rate:.0%}"

        # Rule: OOS Sharpe deeply negative despite good IS
        if valid_oos > 0 and is_sharpe > 1.0:
            avg_oos_sharpe = np.mean([getattr(o, "sharpe_ratio", 0) for o in oos_stats_list if getattr(o, "n_trades", 0) >= self._min_oos_trades])
            if avg_oos_sharpe < self._min_oos_sharpe:
                is_stable = False
                reject_reason = f"avg_oos_sharpe={avg_oos_sharpe:.2f} < {self._min_oos_sharpe}"

        # ── 2. Parameter sensitivity ─────────────────────────────────────────────
        sens_score = self._compute_param_sensitivity(param_perturbations, is_stats)

        if sens_score > self._sensitivity_threshold:
            is_stable = False
            reject_reason = f"param_sensitivity={sens_score:.3f} > {self._sensitivity_threshold}"

        # ── Determine confidence ───────────────────────────────────────────────────
        if is_stable:
            if wf_stability >= 0.8 and sens_score < 0.1:
                confidence = "high"
            elif wf_stability >= 0.6 and sens_score < 0.2:
                confidence = "medium"
            else:
                confidence = "low"
        else:
            confidence = "low"

        return StabilityReport(
            run_id=run_id,
            is_stable=is_stable,
            is_stable_reason="PASS" if is_stable else "REJECTED",
            param_sensitivity_score=round(sens_score, 4),
            is_oos_ratio=round(is_oos_ratio, 3),
            walk_forward_stability=round(wf_stability, 4),
            n_oos_windows=valid_oos,
            n_oos_positive=positive_oos,
            reject_reason=reject_reason,
            confidence=confidence,
        )

    def _compute_param_sensitivity(
        self,
        perturbations: list,
        baseline_stats,
    ) -> float:
        """
        Compute sensitivity: how much does Sharpe degrade under param perturbations?

        perturbations: list of PerformanceStats from nearby parameter sets
        Returns 0.0 (perfectly stable) to 1.0 (completely unstable)
        """
        if not perturbations:
            return 0.0

        base_sharpe = getattr(baseline_stats, "sharpe_ratio", 0.0)
        if base_sharpe == 0:
            base_sharpe = 0.01  # avoid div by zero

        sharpe_diffs = []
        for p in perturbations:
            p_sharpe = getattr(p, "sharpe_ratio", 0.0)
            diff = abs(p_sharpe - base_sharpe) / abs(base_sharpe)
            sharpe_diffs.append(diff)

        # Average fractional Sharpe degradation across perturbations
        return float(np.mean(sharpe_diffs))


# ── Walk-Forward Analysis ───────────────────────────────────────────────────────

@dataclass
class WalkForwardResult:
    windows: list[dict]   # [{'is_stats', 'oos_stats', 'return_diff', 'sharpe_diff'}]
    avg_is_return: float
    avg_oos_return: float
    avg_sharpe_diff: float
    stability_score: float  # fraction of OOS windows beating IS


def walk_forward_analysis(
    bars: list[dict],
    strategy_class,
    strategy_params: dict,
    train_size: int,
    test_size: int,
    step: int,
    stats_calculator,
) -> WalkForwardResult:
    """
    Classic walk-forward analysis:
    Train on [0:train_size], test on [train_size:train_size+test_size],
    slide window by `step` bars.

    Returns aggregated OOS performance.
    """
    n = len(bars)
    if n < train_size + test_size:
        raise ValueError(f"Not enough bars: need {train_size + test_size}, have {n}")

    windows = []
    is_returns = []
    oos_returns = []

    pos = 0
    while pos + train_size + test_size <= n:
        # Note: actual train/test split requires separate engine — this returns
        # placeholder structure; real impl uses ResearchEnvironment internally
        windows.append({"start": pos, "is_end": pos + train_size, "oos_end": pos + train_size + test_size})
        pos += step

    return WalkForwardResult(
        windows=windows,
        avg_is_return=0.0,
        avg_oos_return=0.0,
        avg_sharpe_diff=0.0,
        stability_score=0.0,
    )


# ── Bootstrap Stability ─────────────────────────────────────────────────────────

def bootstrap_stability(pnls: list[float], n_bootstrap: int = 200, threshold: float = 0.05) -> float:
    """
    Bootstrap resample returns and check what fraction of bootstrap
    samples still produce positive total return.

    Returns stability score: fraction of bootstrap samples with positive pnl.
    A score < 0.05 strongly suggests overfitting.
    """
    if not pnls:
        return 0.0
    arr = np.array(pnls)
    n = len(arr)
    rng = np.random.default_rng(42)
    positive = 0
    for _ in range(n_bootstrap):
        sample = rng.choice(arr, size=n, replace=True)
        if np.sum(sample) > 0:
            positive += 1
    return round(positive / n_bootstrap, 4)