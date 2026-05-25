"""
resilience_taxonomy.py — Cascade Resilience Classification Engine
=================================================================
Formal outcome taxonomy and phase transition detection for distributed
system recovery experiments.

Outcome Taxonomy (6-class deterministic classification):
  1. full_recovery          — All nodes operational, stable convergence
  2. partial_recovery       — Majority operational, bounded degradation
  3. oscillatory_instability — Sustained oscillation without full collapse
  4. secondary_collapse      — Partial recovery then cascade failure
  5. cascading_fragmentation — Progressive failure spread beyond initial blast
  6. unrecoverable_partition — Network splits, no re-convergence possible

Phase Detection (7-state machine):
  stable | degradation | fragmentation | recovery_attempt
  stabilized | unstable_equilibrium | collapse

All classification thresholds are deterministic functions of the
simulation telemetry record. No stochastic components in classification.

Usage:
  from simulations.resilience_taxonomy import OutcomeClassifier, PhaseDetector

  classifier = OutcomeClassifier()
  outcome = classifier.classify(metrics_history, config)

  detector = PhaseDetector(config)
  phases = detector.detect_transitions(metrics_history)
  detector.export_transitions_csv(phases, "metrics/phase_transitions.csv")
"""

from __future__ import annotations

import csv
import json
import math
import os
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .recovery_engine import SimulationMetrics


# ─────────────────────────────────────────────────────────────────────────────
# Outcome Taxonomy
# ─────────────────────────────────────────────────────────────────────────────

OUTCOME_CLASSES = [
    "full_recovery",
    "partial_recovery",
    "oscillatory_instability",
    "secondary_collapse",
    "cascading_fragmentation",
    "unrecoverable_partition",
]

OUTCOME_SEVERITY = {
    "full_recovery": 0,
    "partial_recovery": 1,
    "oscillatory_instability": 2,
    "secondary_collapse": 3,
    "cascading_fragmentation": 4,
    "unrecoverable_partition": 5,
}


@dataclass
class StabilitySignatures:
    """Computed stability indicators from a telemetry history."""
    initial_stability: float
    final_stability: float
    min_stability: float
    stability_trend: float          # slope of linear fit over recovery window
    oscillation_count: int           # zero-crossings of stability derivative
    oscillation_amplitude: float     # mean peak-to-trough of stability oscillation
    fragmentation_peak: int          # max fragmented_nodes observed
    fragmentation_duration_ticks: int
    fragmentation_persistence: float # fraction of recovery window with frag > 0
    retry_peak: int
    retry_storm_duration_ticks: int
    recovery_convergence_slope: float # stability gain per tick in recovery phase
    health_final: float
    health_trend: float             # slope over full run
    latency_p95_peak: float
    latency_p95_trend: float        # post-frag latency recovery slope
    active_nodes_final: int
    active_nodes_initial: int
    collapse_velocity: float         # stability drop rate during fragmentation
    secondary_failure_tick: Optional[int]  # tick of first secondary node frag


@dataclass
class ClassificationEvidence:
    """Human-readable evidence trail for a classification decision."""
    outcome: str
    primary_signal: str
    secondary_signals: List[str]
    stability_signature: StabilitySignatures
    violating_thresholds: List[str]  # which recovery criteria were violated
    phase_sequence: List[str]        # detected phase transition sequence


class OutcomeClassifier:
    """
    Deterministic 6-class outcome classifier.

    Classification is based on a vector of stability indicators extracted
    from the full telemetry history. No randomness — same input always
    produces the same classification.

    Threshold values are designed to be:
    - stable_recovery:    stability > 0.90, frag_peak = 0, retry_peak < 5
    - partial_recovery:   stability > 0.50, frag_peak < 30% nodes, stable convergence
    - oscillatory:        3+ oscillation cycles, stability 0.2–0.8 range, no full collapse
    - secondary_collapse: initial recovery then stability drops > 0.3 below peak
    - cascading_frag:     fragmentation grows monotonically after initial frag
    - partitioned:        fragmentation_persistence = 1.0 (never resolved)
    """

    # Thresholds — deterministic, no tuning knobs at runtime
    STABILITY_FULL_RECOVERY = 0.90
    STABILITY_PARTIAL_RECOVERY = 0.50
    STABILITY_COLLAPSE_THRESHOLD = 0.20
    RETRY_STORM_THRESHOLD = 15
    FRAG_PEAK_FRACTION_COLLAPSE = 0.70
    OSCILLATION_COUNT_THRESHOLD = 3
    OSCILLATION_MIN_AMPLITUDE = 0.15
    SECONDARY_DROP_THRESHOLD = 0.30
    CONVERGENCE_SLOPE_RECOVERY = 0.005  # min stability gain/tick to count as recovering

    def classify(
        self,
        metrics_history: List[SimulationMetrics],
        config: Optional[dict] = None,
    ) -> Tuple[str, ClassificationEvidence]:
        """
        Classify the outcome of a simulation run.

        Returns:
            (outcome_string, ClassificationEvidence)
        """
        if not metrics_history:
            return "unknown", ClassificationEvidence(
                outcome="unknown",
                primary_signal="no telemetry data",
                secondary_signals=[],
                stability_signature=self._null_signature(),
                violating_thresholds=["no_data"],
                phase_sequence=[],
            )

        sig = self._compute_signatures(metrics_history, config)
        total_nodes = config.get("topology", {}).get("nodes", 12) if config else 12

        # ── Classification logic (ordered by precedence) ────────────────────

        # 1. Unrecoverable partition: frag never resolved throughout recovery window
        if sig.fragmentation_persistence >= 1.0 and sig.final_stability < 0.30:
            outcome = "unrecoverable_partition"
            primary = (
                f"fragmentation_persistence={sig.fragmentation_persistence:.2f} "
                f"(never resolved), final_stability={sig.final_stability:.3f}"
            )
            violating = ["fragmentation_persistence", "final_stability"]
            phase_seq = self._detect_phase_sequence(metrics_history)

        # 2. Cascading fragmentation: monotonic frag growth after initial failure
        elif self._is_cascading_fragmentation(sig, metrics_history):
            outcome = "cascading_fragmentation"
            primary = (
                f"frag_peak={sig.fragmentation_peak} at tick "
                f"{self._find_frag_peak_tick(metrics_history)}, "
                f"monotonic spread post-initial-failure"
            )
            violating = ["fragmentation_monominic_spread"]
            phase_seq = self._detect_phase_sequence(metrics_history)

        # 3. Secondary collapse: partial recovery then drop
        elif self._is_secondary_collapse(sig, metrics_history):
            outcome = "secondary_collapse"
            primary = (
                f"stability dropped {sig.secondary_failure_tick} ticks after recovery onset, "
                f"drop magnitude={sig.collapse_velocity:.3f}/tick"
            )
            violating = ["secondary_failure", "collapse_velocity"]
            phase_seq = self._detect_phase_sequence(metrics_history)

        # 4. Oscillatory instability: 3+ cycles, sustained but no full collapse
        elif (sig.oscillation_count >= self.OSCILLATION_COUNT_THRESHOLD
              and sig.oscillation_amplitude >= self.OSCILLATION_MIN_AMPLITUDE
              and sig.final_stability >= self.STABILITY_COLLAPSE_THRESHOLD):
            outcome = "oscillatory_instability"
            primary = (
                f"oscillation_count={sig.oscillation_count}, "
                f"amplitude={sig.oscillation_amplitude:.3f}, "
                f"stability_trend={sig.stability_trend:.4f}"
            )
            violating = ["oscillation_count", "oscillation_amplitude"]
            phase_seq = self._detect_phase_sequence(metrics_history)

        # 5. Full recovery: near-perfect convergence
        elif (sig.final_stability >= self.STABILITY_FULL_RECOVERY
              and sig.fragmentation_peak == 0
              and sig.retry_peak < 5
              and sig.oscillation_count == 0):
            outcome = "full_recovery"
            primary = (
                f"final_stability={sig.final_stability:.3f}, "
                f"frag_peak=0, retry_peak={sig.retry_peak}"
            )
            violating = []
            phase_seq = self._detect_phase_sequence(metrics_history)

        # 6. Partial recovery: majority stable, bounded degradation
        elif (sig.final_stability >= self.STABILITY_PARTIAL_RECOVERY
              and sig.fragmentation_peak < total_nodes * self.FRAG_PEAK_FRACTION_COLLAPSE
              and sig.recovery_convergence_slope >= 0):
            outcome = "partial_recovery"
            primary = (
                f"final_stability={sig.final_stability:.3f}, "
                f"frag_peak={sig.fragmentation_peak}/{total_nodes}, "
                f"convergence_slope={sig.recovery_convergence_slope:.4f}"
            )
            violating = []
            phase_seq = self._detect_phase_sequence(metrics_history)

        # 7. Fallback: secondary collapse (worst-case when other criteria fail)
        else:
            outcome = "secondary_collapse"
            primary = (
                f"fallback: final_stability={sig.final_stability:.3f}, "
                f"frag_peak={sig.fragmentation_peak}, "
                f"recovery_convergence_slope={sig.recovery_convergence_slope:.4f}"
            )
            violating = ["stability", "frag_peak", "convergence_slope"]
            phase_seq = self._detect_phase_sequence(metrics_history)

        secondary = self._build_secondary_signals(sig)

        return outcome, ClassificationEvidence(
            outcome=outcome,
            primary_signal=primary,
            secondary_signals=secondary,
            stability_signature=sig,
            violating_thresholds=violating,
            phase_sequence=phase_seq,
        )

    # ── Signature computation ─────────────────────────────────────────────────

    def _compute_signatures(
        self,
        history: List[SimulationMetrics],
        config: Optional[dict],
    ) -> StabilitySignatures:
        """Extract all stability indicators from telemetry history."""
        if not history:
            return self._null_signature()

        stabilities = [m.stability_score for m in history]
        frag_counts = [m.fragmented_nodes for m in history]
        retries = [m.retry_count for m in history]
        healths = [m.global_health_score for m in history]
        p95_lats = [m.p50_latency for m in history]  # use p50 as proxy for p95 dynamics
        active = [m.active_nodes for m in history]

        total_nodes = config.get("topology", {}).get("nodes", 12) if config else 12

        # Stability window analysis
        initial_stability = stabilities[0] if stabilities else 0.0
        final_stability = stabilities[-1] if stabilities else 0.0
        min_stability = min(stabilities) if stabilities else 0.0

        # Stability trend (linear regression slope over recovery window)
        recovery_start_idx = self._find_recovery_start_idx(history)
        recovery_history = history[recovery_start_idx:]
        if len(recovery_history) >= 5:
            stability_trend = self._linear_slope(
                [m.tick for m in recovery_history],
                [m.stability_score for m in recovery_history],
            )
            recovery_convergence_slope = max(0, stability_trend)
        else:
            stability_trend = 0.0
            recovery_convergence_slope = 0.0

        # Oscillation detection
        osc_count, osc_amplitude = self._detect_oscillations(
            [m.stability_score for m in history],
            [m.tick for m in history],
        )

        # Fragmentation analysis
        frag_peak = max(frag_counts) if frag_counts else 0
        frag_ticks = [i for i, f in enumerate(frag_counts) if f > 0]
        frag_duration = len(frag_ticks) if frag_ticks else 0

        # Fragmentation persistence: fraction of post-frag window with active frag
        recovery_window = history[recovery_start_idx:] if recovery_start_idx else history
        recovery_frag_counts = [m.fragmented_nodes for m in recovery_window]
        frag_persistence = (
            sum(1 for f in recovery_frag_counts if f > 0) / max(len(recovery_frag_counts), 1)
            if recovery_frag_counts else 0.0
        )

        # Retry storm analysis
        retry_peak = max(retries) if retries else 0
        retry_storm_start = next((i for i, r in enumerate(retries) if r > self.RETRY_STORM_THRESHOLD), None)
        retry_storm_end = None
        if retry_storm_start is not None:
            for i in range(len(retries) - 1, retry_storm_start - 1, -1):
                if retries[i] > self.RETRY_STORM_THRESHOLD:
                    retry_storm_end = i
                    break
        retry_storm_duration = (retry_storm_end - retry_storm_start + 1) if (retry_storm_start and retry_storm_end) else 0

        # Health trend
        health_trend = self._linear_slope(
            [m.tick for m in history],
            healths,
        )

        # Latency dynamics
        latency_p95_peak = max(p95_lats) if p95_lats else 0.0
        post_frag_idx = next((i for i, f in enumerate(frag_counts) if f > 0), None)
        if post_frag_idx is not None and len(history) > post_frag_idx + 5:
            latency_trend = self._linear_slope(
                [m.tick for m in history[post_frag_idx:]],
                p95_lats[post_frag_idx:],
            )
        else:
            latency_trend = 0.0

        # Collapse velocity (stability drop rate during fragmentation)
        frag_start_idx = next((i for i, f in enumerate(frag_counts) if f > 0), None)
        collapse_velocity = 0.0
        secondary_failure_tick = None
        if frag_start_idx is not None:
            pre_frag_stability = stabilities[frag_start_idx - 1] if frag_start_idx > 0 else stabilities[0]
            window = history[frag_start_idx:min(frag_start_idx + 20, len(history))]
            if len(window) >= 3:
                collapse_velocity = self._linear_slope(
                    [m.tick for m in window],
                    [m.stability_score for m in window],
                )
                collapse_velocity = min(0, collapse_velocity)  # negative = dropping

        # Active nodes
        active_initial = active[0] if active else total_nodes
        active_final = active[-1] if active else 0

        return StabilitySignatures(
            initial_stability=initial_stability,
            final_stability=final_stability,
            min_stability=min_stability,
            stability_trend=stability_trend,
            oscillation_count=osc_count,
            oscillation_amplitude=osc_amplitude,
            fragmentation_peak=frag_peak,
            fragmentation_duration_ticks=frag_duration,
            fragmentation_persistence=frag_persistence,
            retry_peak=retry_peak,
            retry_storm_duration_ticks=retry_storm_duration,
            recovery_convergence_slope=recovery_convergence_slope,
            health_final=healths[-1] if healths else 0.0,
            health_trend=health_trend,
            latency_p95_peak=latency_p95_peak,
            latency_p95_trend=latency_trend,
            active_nodes_final=active_final,
            active_nodes_initial=active_initial,
            collapse_velocity=collapse_velocity,
            secondary_failure_tick=secondary_failure_tick,
        )

    def _null_signature(self) -> StabilitySignatures:
        return StabilitySignatures(
            initial_stability=0.0, final_stability=0.0, min_stability=0.0,
            stability_trend=0.0, oscillation_count=0, oscillation_amplitude=0.0,
            fragmentation_peak=0, fragmentation_duration_ticks=0,
            fragmentation_persistence=0.0, retry_peak=0, retry_storm_duration_ticks=0,
            recovery_convergence_slope=0.0, health_final=0.0, health_trend=0.0,
            latency_p95_peak=0.0, latency_p95_trend=0.0,
            active_nodes_final=0, active_nodes_initial=0,
            collapse_velocity=0.0, secondary_failure_tick=None,
        )

    def _find_recovery_start_idx(self, history: List[SimulationMetrics]) -> int:
        """Find the tick index where recovery mechanisms activate."""
        fi = {}
        if history:
            # Approximate: recovery tick is typically around tick 280 in standard configs
            # Use heuristic: first tick where retry_count > threshold * 0.5
            for i, m in enumerate(history):
                if m.tick >= 280:
                    return i
        return max(0, len(history) // 2)

    def _detect_oscillations(self, values: List[float], ticks: List[int]) -> Tuple[int, float]:
        """Count zero-crossings and mean amplitude of oscillations."""
        if len(values) < 6:
            return 0, 0.0

        # Compute first derivative
        derivatives = [values[i] - values[i - 1] for i in range(1, len(values))]

        # Count sign changes in derivative (zero-crossings)
        crossings = 0
        for i in range(1, len(derivatives)):
            if derivatives[i] * derivatives[i - 1] < 0:
                crossings += 1

        osc_count = crossings // 2  # each oscillation has 2 crossings

        # Compute mean peak-to-trough amplitude in detected cycles
        amplitudes = []
        in_up = derivatives[0] > 0 if derivatives else False
        peak = values[0]
        trough = values[0]
        for i in range(1, len(derivatives)):
            if derivatives[i] > 0 and not in_up:
                # Start of upward phase — trough just passed
                if trough < peak:
                    amplitudes.append(peak - trough)
                trough = values[i]
                in_up = True
            elif derivatives[i] < 0 and in_up:
                # Start of downward phase — peak just passed
                if peak > trough:
                    amplitudes.append(peak - trough)
                peak = values[i]
                in_up = False
            peak = max(peak, values[i])
            trough = min(trough, values[i])

        osc_amplitude = statistics.mean(amplitudes) if amplitudes else 0.0
        return osc_count, osc_amplitude

    def _linear_slope(self, xs: List[float], ys: List[float]) -> float:
        """Compute linear regression slope (OLS)."""
        if len(xs) < 2 or len(ys) < 2 or len(xs) != len(ys):
            return 0.0
        n = len(xs)
        x_mean = sum(xs) / n
        y_mean = sum(ys) / n
        num = sum((xs[i] - x_mean) * (ys[i] - y_mean) for i in range(n))
        den = sum((xs[i] - x_mean) ** 2 for i in range(n))
        return num / den if den != 0 else 0.0

    def _is_cascading_fragmentation(
        self,
        sig: StabilitySignatures,
        history: List[SimulationMetrics],
    ) -> bool:
        """Fragmentation grows monotonically after first fragmentation event."""
        frag_counts = [m.fragmented_nodes for m in history]
        first_frag_idx = next((i for i, f in enumerate(frag_counts) if f > 0), None)
        if first_frag_idx is None:
            return False

        post_frag = frag_counts[first_frag_idx:]
        if len(post_frag) < 5:
            return False

        # Check monotonic or increasing trend
        trend = self._linear_slope(list(range(len(post_frag))), post_frag)
        return trend > 0.1 and sig.fragmentation_peak > 3

    def _is_secondary_collapse(
        self,
        sig: StabilitySignatures,
        history: List[SimulationMetrics],
    ) -> bool:
        """Detect recovery followed by a secondary stability drop > threshold."""
        stabilities = [m.stability_score for m in history]
        if len(stabilities) < 10:
            return False

        frag_counts = [m.fragmented_nodes for m in history]
        recovery_start = next((i for i, f in enumerate(frag_counts) if f > 0), None)
        if recovery_start is None:
            return False

        post_recovery = stabilities[recovery_start:]
        if len(post_recovery) < 10:
            return False

        # Find peak stability after recovery starts
        peak_idx = recovery_start + post_recovery.index(max(post_recovery))
        post_peak = stabilities[peak_idx:]

        if len(post_peak) < 5:
            return False

        # Secondary collapse: drop of > 0.3 from post-recovery peak
        peak_val = max(post_recovery)
        final_val = stabilities[-1]
        drop = peak_val - final_val

        # Also check if there was a clear recovery then drop pattern
        mid_idx = len(post_recovery) // 2
        first_half_mean = statistics.mean(post_recovery[:mid_idx]) if mid_idx > 0 else 0
        second_half_mean = statistics.mean(post_recovery[mid_idx:]) if mid_idx < len(post_recovery) else 0

        return drop > self.SECONDARY_DROP_THRESHOLD or second_half_mean < first_half_mean - 0.15

    def _find_frag_peak_tick(self, history: List[SimulationMetrics]) -> Optional[int]:
        frag_counts = [m.fragmented_nodes for m in history]
        if not frag_counts:
            return None
        peak_val = max(frag_counts)
        peak_idx = frag_counts.index(peak_val)
        return history[peak_idx].tick if peak_idx < len(history) else None

    def _detect_phase_sequence(self, history: List[SimulationMetrics]) -> List[str]:
        detector = PhaseDetector({})
        return detector.detect_transitions(history)

    def _build_secondary_signals(self, sig: StabilitySignatures) -> List[str]:
        signals = []
        if sig.fragmentation_persistence > 0.5:
            signals.append(f"frag_persistence={sig.fragmentation_persistence:.2f}")
        if sig.retry_peak > self.RETRY_STORM_THRESHOLD:
            signals.append(f"retry_storm_peak={sig.retry_peak}")
        if sig.collapse_velocity < -0.02:
            signals.append(f"collapse_velocity={sig.collapse_velocity:.4f}")
        if sig.oscillation_count > 0:
            signals.append(f"oscillations={sig.oscillation_count}")
        if sig.latency_p95_trend > 0:
            signals.append(f"latency_recovery_trend={sig.latency_p95_trend:.4f}")
        if sig.health_trend < -0.001:
            signals.append(f"health_decline_trend={sig.health_trend:.4f}")
        return signals


# ─────────────────────────────────────────────────────────────────────────────
# Phase Transition Detection
# ─────────────────────────────────────────────────────────────────────────────

PHASE_LABELS = [
    "stable",
    "degradation",
    "fragmentation",
    "recovery_attempt",
    "stabilized",
    "unstable_equilibrium",
    "collapse",
]


@dataclass
class PhaseTransition:
    """Record of one phase transition event."""
    from_phase: str
    to_phase: str
    tick: int
    time_ms: float
    stability_score: float
    fragmented_nodes: int
    retry_count: int
    trigger: str  # what caused the transition


class PhaseDetector:
    """
    7-state phase detection state machine.

    State transitions are driven by thresholds on observable telemetry
    signals. The detector processes tick-by-tick metrics and identifies
    the exact tick of each phase boundary.

    State machine:

      stable ──► degradation ──► fragmentation ──► recovery_attempt
                   │                   │                 │
                   ▼                   ▼                 ▼
               collapse          unstable_equilibrium   stabilized
    """

    # Phase boundaries — deterministic thresholds
    DEGRADATION_STABILITY_THRESHOLD = 0.85   # stability below this = degradation
    FRAGMENTATION_FRAG_THRESHOLD = 1          # any fragmented node = fragmentation
    RECOVERY_STABILITY_RISE = 0.05            # stability gain of this much = recovery_attempt
    STABILIZED_RECOVERY_SLOPE = 0.003         # min positive slope in recovery = stabilized
    UNSTABLE_EQ_RETRY_THRESHOLD = 10         # retry > this in recovery = unstable_eq
    COLLAPSE_STABILITY_THRESHOLD = 0.15      # stability below this = collapse

    def __init__(self, config: dict):
        self.config = config
        self._reset()

    def _reset(self):
        self.current_phase: str = "stable"
        self.transitions: List[PhaseTransition] = []
        self._last_stability: float = 1.0
        self._last_frag_count: int = 0
        self._recovery_base_stability: float = 0.0
        self._recovery_started: bool = False

    def detect_transitions(
        self,
        metrics_history: List[SimulationMetrics],
    ) -> List[str]:
        """
        Process full telemetry history and return ordered list of phase labels.

        The returned list has one entry per tick, representing the phase
        that was active at that tick. Phase transitions are marked by
        changes in the returned list.
        """
        self._reset()
        phases = []

        for m in metrics_history:
            new_phase = self._compute_phase(m)
            if new_phase != self.current_phase:
                self._record_transition(m, new_phase)
            phases.append(new_phase)
            self.current_phase = new_phase

        return phases

    def _compute_phase(self, m: SimulationMetrics) -> str:
        """Determine phase for a single tick based on telemetry."""
        frag = m.fragmented_nodes
        stab = m.stability_score
        retry = m.retry_count

        # Collapse: terminal state, no exit
        if self.current_phase == "collapse":
            return "collapse"

        # stable: all nominal
        if self.current_phase == "stable":
            if stab < self.DEGRADATION_STABILITY_THRESHOLD:
                return "degradation"
            return "stable"

        # degradation: stability degraded but no fragmentation yet
        if self.current_phase == "degradation":
            if frag > 0:
                return "fragmentation"
            if stab < self.COLLAPSE_STABILITY_THRESHOLD:
                return "collapse"
            if stab >= self.DEGRADATION_STABILITY_THRESHOLD:
                return "stable"
            return "degradation"

        # fragmentation: active node failures
        if self.current_phase == "fragmentation":
            if frag == 0 and stab >= self.STABILIZED_RECOVERY_SLOPE * 100:
                return "recovery_attempt"
            if stab < self.COLLAPSE_STABILITY_THRESHOLD:
                return "collapse"
            return "fragmentation"

        # recovery_attempt: recovery mechanisms active
        if self.current_phase == "recovery_attempt":
            # Check for stabilization: positive convergence slope
            stability_delta = stab - self._recovery_base_stability
            if stab >= 0.80 and frag == 0:
                return "stabilized"
            if retry > self.UNSTABLE_EQ_RETRY_THRESHOLD:
                return "unstable_equilibrium"
            if stab < self.COLLAPSE_STABILITY_THRESHOLD:
                return "collapse"
            if frag > 0 and stability_delta < 0:
                return "fragmentation"
            return "recovery_attempt"

        # stabilized: successful recovery
        if self.current_phase == "stabilized":
            if retry > self.UNSTABLE_EQ_RETRY_THRESHOLD:
                return "unstable_equilibrium"
            if stab < self.DEGRADATION_STABILITY_THRESHOLD:
                return "degradation"
            return "stabilized"

        # unstable_equilibrium: partial recovery with sustained retries
        if self.current_phase == "unstable_equilibrium":
            if frag == 0 and stab >= 0.80:
                return "stabilized"
            if stab < self.COLLAPSE_STABILITY_THRESHOLD:
                return "collapse"
            if retry <= self.UNSTABLE_EQ_RETRY_THRESHOLD and stab >= 0.60:
                return "recovery_attempt"
            return "unstable_equilibrium"

        return "stable"

    def _record_transition(self, m: SimulationMetrics, new_phase: str):
        old_phase = self.current_phase

        # Determine trigger description
        if new_phase == "degradation":
            trigger = f"stability_dropped_to={m.stability_score:.3f}"
        elif new_phase == "fragmentation":
            trigger = f"frag_count={m.fragmented_nodes}"
        elif new_phase == "recovery_attempt":
            self._recovery_base_stability = m.stability_score
            self._recovery_started = True
            trigger = f"recovery_onset_stability={m.stability_score:.3f}"
        elif new_phase == "stabilized":
            trigger = f"frag_cleared_stability={m.stability_score:.3f}"
        elif new_phase == "unstable_equilibrium":
            trigger = f"retry_volume={m.retry_count}"
        elif new_phase == "collapse":
            trigger = f"stability_collapsed_to={m.stability_score:.3f}"
        else:
            trigger = "stability_normalized"

        self.transitions.append(PhaseTransition(
            from_phase=old_phase,
            to_phase=new_phase,
            tick=m.tick,
            time_ms=m.time_ms,
            stability_score=m.stability_score,
            fragmented_nodes=m.fragmented_nodes,
            retry_count=m.retry_count,
            trigger=trigger,
        ))

    def export_transitions_csv(
        self,
        phases_per_tick: List[str],
        path: str,
        metrics_history: Optional[List[SimulationMetrics]] = None,
    ):
        """
        Export phase sequence and transition events to CSV.

        Phase sequence CSV columns:
          tick, time_ms, phase, stability_score, fragmented_nodes,
          retry_count, is_transition
        """
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        if metrics_history is None:
            # Reconstruct a minimal history from phases_per_tick
            rows = []
            for i, phase in enumerate(phases_per_tick):
                rows.append({
                    "tick": i,
                    "time_ms": i * 50,
                    "phase": phase,
                    "stability_score": "",
                    "fragmented_nodes": "",
                    "retry_count": "",
                    "is_transition": "",
                })
        else:
            transition_set = {(t.from_phase, t.to_phase, t.tick) for t in self.transitions}
            rows = []
            for m, phase in zip(metrics_history, phases_per_tick):
                is_trans = any(
                    t.to_phase == phase and t.tick == m.tick
                    for t in self.transitions
                )
                rows.append({
                    "tick": m.tick,
                    "time_ms": m.time_ms,
                    "phase": phase,
                    "stability_score": round(m.stability_score, 4),
                    "fragmented_nodes": m.fragmented_nodes,
                    "retry_count": m.retry_count,
                    "is_transition": "TRUE" if is_trans else "",
                })

        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["tick", "time_ms", "phase", "stability_score",
                            "fragmented_nodes", "retry_count", "is_transition"],
            )
            writer.writeheader()
            writer.writerows(rows)

    def export_transition_events_csv(self, path: str):
        """Export only the transition events (not full tick sequence)."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        rows = [
            {
                "from_phase": t.from_phase,
                "to_phase": t.to_phase,
                "tick": t.tick,
                "time_ms": t.time_ms,
                "stability_score": round(t.stability_score, 4),
                "fragmented_nodes": t.fragmented_nodes,
                "retry_count": t.retry_count,
                "trigger": t.trigger,
            }
            for t in self.transitions
        ]
        if rows:
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=["from_phase", "to_phase", "tick", "time_ms",
                                "stability_score", "fragmented_nodes",
                                "retry_count", "trigger"],
                )
                writer.writeheader()
                writer.writerows(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Stability Mapper (Monte Carlo 2D parameter space analysis)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StabilityRegion:
    """A region in parameter space with homogeneous stability characteristics."""
    x_param: str
    y_param: str
    x_bins: List[float]
    y_bins: List[float]
    outcome_counts: Dict[str, int]          # outcome -> count in this cell
    mean_stability: float
    mean_health: float
    mean_frag_duration_ms: float
    sample_count: int
    region_type: str                        # stable | oscillation | frag_boundary | unstable


class StabilityMapper:
    """
    2D parameter-space mapper for stability landscape visualization.

    For each pair of parameters (x, y), bins the experiment results and
    computes the dominant outcome and mean stability scores.

    Exports:
      - stability_maps/          — per-topology heatmap data
      - resilience_frontiers/    — boundary conditions
      - phase_boundary_data.csv  — all boundary points
    """

    def __init__(self):
        self.regions: Dict[Tuple[str, str], StabilityRegion] = {}
        self.boundary_points: List[dict] = []

    def map_experiments(
        self,
        results: List[dict],
        x_param: str,
        y_param: str,
        x_bins: Optional[List[float]] = None,
        y_bins: Optional[List[float]] = None,
    ) -> Dict[Tuple[str, str], StabilityRegion]:
        """
        Bin experiment results by (x_param, y_param) and compute stability regions.

        Args:
            results: List of run result dicts with outcome, final_stability, etc.
            x_param: Parameter for x-axis (e.g., "recovery_rate", "latency_multiplier")
            y_param: Parameter for y-axis
            x_bins:  Bin edges for x parameter (auto-computed if None)
            y_bins:  Bin edges for y parameter (auto-computed if None)

        Returns:
            Dict mapping (x_bin_label, y_bin_label) -> StabilityRegion
        """
        # Collect parameter values to determine bin edges
        x_vals = [r.get(x_param, 0) for r in results if r.get(x_param) is not None]
        y_vals = [r.get(y_param, 0) for r in results if r.get(y_param) is not None]

        if not x_vals or not y_vals:
            return {}

        if x_bins is None:
            x_bins = self._compute_bins(x_vals)
        if y_bins is None:
            y_bins = self._compute_bins(y_vals)

        # Bin results
        cells: Dict[Tuple[int, int], List[dict]] = {}
        for r in results:
            x_val = r.get(x_param)
            y_val = r.get(y_param)
            if x_val is None or y_val is None:
                continue
            x_bin = self._find_bin(x_val, x_bins)
            y_bin = self._find_bin(y_val, y_bins)
            if x_bin is not None and y_bin is not None:
                cells.setdefault((x_bin, y_bin), []).append(r)

        # Compute region statistics for each cell
        regions = {}
        for (x_bin, y_bin), cell_results in cells.items():
            region_type = self._classify_cell(cell_results)
            stabilities = [r.get("final_stability", 0) for r in cell_results
                          if r.get("final_stability") is not None]
            healths = [r.get("final_health", 0) for r in cell_results
                       if r.get("final_health") is not None]
            frag_durations = [r.get("fragmentation_duration_ms", 0) for r in cell_results
                              if r.get("fragmentation_duration_ms") is not None]

            outcome_counts: Dict[str, int] = {}
            for r in cell_results:
                o = r.get("outcome", "unknown")
                outcome_counts[o] = outcome_counts.get(o, 0) + 1

            regions[(x_bin, y_bin)] = StabilityRegion(
                x_param=x_param,
                y_param=y_param,
                x_bins=x_bins,
                y_bins=y_bins,
                outcome_counts=outcome_counts,
                mean_stability=statistics.mean(stabilities) if stabilities else 0.0,
                mean_health=statistics.mean(healths) if healths else 0.0,
                mean_frag_duration_ms=statistics.mean(frag_durations) if frag_durations else 0.0,
                sample_count=len(cell_results),
                region_type=region_type,
            )

        self.regions = regions
        self._compute_boundaries(x_bins, y_bins, regions)
        return regions

    def _compute_bins(self, values: List[float], n_bins: int = 6) -> List[float]:
        """Compute quantile-based bin edges."""
        if len(values) <= n_bins:
            return sorted(set(values))
        sorted_vals = sorted(values)
        step = len(sorted_vals) // n_bins
        bins = []
        for i in range(n_bins):
            idx = min(i * step, len(sorted_vals) - 1)
            bins.append(sorted_vals[idx])
        bins.append(sorted_vals[-1])
        return sorted(set(bins))

    def _find_bin(self, value: float, bins: List[float]) -> Optional[int]:
        """Find bin index for a value. Returns None if out of range."""
        if value < bins[0] or value > bins[-1]:
            return None
        for i in range(len(bins) - 1):
            if bins[i] <= value < bins[i + 1]:
                return i
        return len(bins) - 2 if bins else None

    def _classify_cell(self, results: List[dict]) -> str:
        """Classify a parameter-space cell by dominant outcome."""
        outcomes = [r.get("outcome", "unknown") for r in results]
        outcome_counts: Dict[str, int] = {}
        for o in outcomes:
            outcome_counts[o] = outcome_counts.get(o, 0) + 1

        dominant = max(outcome_counts, key=outcome_counts.get)
        recovery_outcomes = ["full_recovery", "partial_recovery"]
        oscillation_outcomes = ["oscillatory_instability"]
        collapse_outcomes = ["secondary_collapse", "cascading_fragmentation", "unrecoverable_partition"]

        if dominant in recovery_outcomes:
            return "stable"
        elif dominant in oscillation_outcomes:
            return "oscillation"
        elif dominant in collapse_outcomes:
            if outcome_counts.get("cascading_fragmentation", 0) > 0:
                return "frag_boundary"
            return "unstable"
        return "mixed"

    def _compute_boundaries(
        self,
        x_bins: List[float],
        y_bins: List[float],
        regions: Dict[Tuple[int, int], StabilityRegion],
    ):
        """Extract boundary points between stable and unstable regions."""
        boundary_points = []

        for (x_bin, y_bin), region in regions.items():
            if region.region_type not in ("stable", "oscillation", "unstable", "frag_boundary"):
                continue

            x_center = (x_bins[x_bin] + x_bins[x_bin + 1]) / 2 if x_bin < len(x_bins) - 1 else x_bins[x_bin]
            y_center = (y_bins[y_bin] + y_bins[y_bin + 1]) / 2 if y_bin < len(y_bins) - 1 else y_bins[y_bin]

            # Check if adjacent cell has different region type
            for dx, dy in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
                adj = (x_bin + dx, y_bin + dy)
                if adj in regions:
                    adj_region = regions[adj]
                    if adj_region.region_type != region.region_type:
                        boundary_points.append({
                            "x_param": region.x_param,
                            "y_param": region.y_param,
                            "x_value": x_center,
                            "y_value": y_center,
                            "region_type": region.region_type,
                            "adjacent_region_type": adj_region.region_type,
                            "sample_count": region.sample_count,
                            "mean_stability": round(region.mean_stability, 4),
                            "mean_health": round(region.mean_health, 4),
                            "dominant_outcome": max(region.outcome_counts, key=region.outcome_counts.get),
                        })

        self.boundary_points = boundary_points

    def export_stability_map_csv(
        self,
        x_param: str,
        y_param: str,
        output_dir: str,
        topology: Optional[str] = None,
    ):
        """
        Export a heatmap-compatible CSV of mean_stability per parameter cell.

        Rows: y_bin (parameter y value)
        Columns: x_bin (parameter x value)
        Cell values: mean stability score
        """
        os.makedirs(output_dir, exist_ok=True)

        # Collect all bins
        all_x_bins = sorted(set(k[0] for k in self.regions.keys()))
        all_y_bins = sorted(set(k[1] for k in self.regions.keys()))

        if not all_x_bins or not all_y_bins:
            return

        # Build matrix
        x_bins_edges = list(self.regions.get((all_x_bins[0], all_y_bins[0]), StabilityRegion(
            x_param=x_param, y_param=y_param, x_bins=[], y_bins=[],
            outcome_counts={}, mean_stability=0, mean_health=0,
            mean_frag_duration_ms=0, sample_count=0, region_type=""
        )).x_bins)

        rows = []
        for y_bin in all_y_bins:
            row = []
            for x_bin in all_x_bins:
                region = self.regions.get((x_bin, y_bin))
                if region:
                    row.append(round(region.mean_stability, 4))
                else:
                    row.append("")
            rows.append(row)

        # Export heatmap data
        prefix = f"{topology}_" if topology else ""
        path = os.path.join(output_dir, f"{prefix}stability_map_{x_param}_vs_{y_param}.csv")

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            # Header: x bin centers
            x_centers = []
            for xb in all_x_bins:
                if xb < len(x_bins_edges) - 1:
                    x_centers.append(round((x_bins_edges[xb] + x_bins_edges[xb + 1]) / 2, 4))
                else:
                    x_centers.append(x_bins_edges[xb] if xb < len(x_bins_edges) else xb)
            writer.writerow(["y\\x"] + x_centers)
            for y_bin, row in zip(all_y_bins, rows):
                if y_bin < len(x_bins_edges) - 1:
                    y_center = round((x_bins_edges[y_bin] + x_bins_edges[y_bin + 1]) / 2, 4)
                else:
                    y_center = x_bins_edges[y_bin] if y_bin < len(x_bins_edges) else y_bin
                writer.writerow([y_center] + row)

    def export_boundary_data_csv(self, output_dir: str, topology: Optional[str] = None):
        """Export all detected phase boundary points."""
        os.makedirs(output_dir, exist_ok=True)
        prefix = f"{topology}_" if topology else ""
        path = os.path.join(output_dir, f"{prefix}phase_boundary_data.csv")

        if not self.boundary_points:
            return

        fieldnames = list(self.boundary_points[0].keys())
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self.boundary_points)

    def export_resilience_frontier_csv(
        self,
        output_dir: str,
        topology: Optional[str] = None,
    ):
        """
        Export the resilience frontier: minimum parameter values that
        still produce stable outcomes, per topology.
        """
        os.makedirs(output_dir, exist_ok=True)

        # Find minimum x,y for stable cells
        stable_cells = [
            r for r in self.regions.values() if r.region_type == "stable"
        ]
        if not stable_cells:
            return

        # For each x_bin, find the lowest y_bin that is stable
        by_x_bin: Dict[int, List[StabilityRegion]] = {}
        for r in stable_cells:
            # Find x_bin index
            x_bins_edges = r.x_bins
            # We store by bin index — reconstruct from x_bins
            by_x_bin.setdefault(0, []).append(r)

        frontier_rows = []
        x_bins_edges = list(self.regions.values())[0].x_bins if self.regions else []

        for (x_bin, y_bin), region in self.regions.items():
            if region.region_type == "stable":
                if x_bin < len(x_bins_edges) - 1:
                    x_val = round((x_bins_edges[x_bin] + x_bins_edges[x_bin + 1]) / 2, 4)
                else:
                    x_val = x_bins_edges[x_bin] if x_bin < len(x_bins_edges) else x_bin
                frontier_rows.append({
                    "x_param": region.x_param,
                    "y_param": region.y_param,
                    "x_value": x_val,
                    "y_threshold_min": region.y_bins[y_bin] if y_bin < len(region.y_bins) else None,
                    "region_type": "stable",
                    "sample_count": region.sample_count,
                    "mean_stability": round(region.mean_stability, 4),
                    "mean_health": round(region.mean_health, 4),
                })

        prefix = f"{topology}_" if topology else ""
        path = os.path.join(output_dir, f"{prefix}resilience_frontier.csv")
        if frontier_rows:
            fieldnames = list(frontier_rows[0].keys())
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(frontier_rows)


# ─────────────────────────────────────────────────────────────────────────────
# Combined analysis entry point
# ─────────────────────────────────────────────────────────────────────────────

def classify_and_analyze(
    metrics_history: List[SimulationMetrics],
    config: dict,
    output_dir: str = "metrics",
) -> Tuple[str, ClassificationEvidence, List[PhaseTransition]]:
    """
    Run full classification + phase detection on a simulation run.

    Returns:
        (outcome, evidence, transitions)
    """
    os.makedirs(output_dir, exist_ok=True)

    # Classify outcome
    classifier = OutcomeClassifier()
    outcome, evidence = classifier.classify(metrics_history, config)

    # Detect phases
    detector = PhaseDetector(config)
    phases = detector.detect_transitions(metrics_history)

    # Export transitions
    transitions_path = os.path.join(output_dir, "phase_transitions.csv")
    detector.export_transitions_csv(phases, transitions_path, metrics_history)

    events_path = os.path.join(output_dir, "phase_transition_events.csv")
    detector.export_transition_events_csv(events_path)

    # Export classification evidence
    evidence_path = os.path.join(output_dir, "classification_evidence.json")
    with open(evidence_path, "w") as f:
        json.dump({
            "outcome": evidence.outcome,
            "primary_signal": evidence.primary_signal,
            "secondary_signals": evidence.secondary_signals,
            "violating_thresholds": evidence.violating_thresholds,
            "phase_sequence": evidence.phase_sequence,
            "signature": {
                "initial_stability": evidence.stability_signature.initial_stability,
                "final_stability": evidence.stability_signature.final_stability,
                "stability_trend": evidence.stability_signature.stability_trend,
                "oscillation_count": evidence.stability_signature.oscillation_count,
                "oscillation_amplitude": evidence.stability_signature.oscillation_amplitude,
                "fragmentation_peak": evidence.stability_signature.fragmentation_peak,
                "fragmentation_persistence": evidence.stability_signature.fragmentation_persistence,
                "retry_peak": evidence.stability_signature.retry_peak,
                "retry_storm_duration_ticks": evidence.stability_signature.retry_storm_duration_ticks,
                "recovery_convergence_slope": evidence.stability_signature.recovery_convergence_slope,
                "collapse_velocity": evidence.stability_signature.collapse_velocity,
                "secondary_failure_tick": evidence.stability_signature.secondary_failure_tick,
            },
        }, f, indent=2)

    return outcome, evidence, detector.transitions
