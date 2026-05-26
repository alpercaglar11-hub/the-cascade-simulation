"""
stability_mapper.py — Cascade 2D Stability Landscape Analyzer
============================================================
Generates 2D parameter-space maps showing stable/unstable regions,
fragmentation thresholds, and recovery boundaries from Monte Carlo data.

Exports:
  stability_maps/              — per-topology heatmap CSVs (mean stability per cell)
  resilience_frontiers/         — per-topology minimum-recovery boundary CSVs
  phase_boundary_data.csv       — all detected phase boundary coordinates

Usage:
  mapper = BatchStabilityMapper(batch_dir="experiments/monte_carlo/batch_xxx")
  mapper.compute_all_maps()
  mapper.export_all()
"""

from __future__ import annotations

import csv
import json
import os
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).parent.parent


# ─────────────────────────────────────────────────────────────────────────────
# Stability region classification
# ─────────────────────────────────────────────────────────────────────────────

REGION_TYPES = [
    "stable",       # full or partial recovery dominant
    "oscillation",  # oscillatory instability dominant
    "frag_boundary", # cascading fragmentation dominant
    "unstable",     # secondary collapse / unrecoverable partition dominant
    "mixed",        # no dominant outcome
]


@dataclass
class CellStats:
    x_bin_center: float
    y_bin_center: float
    x_val_min: float
    x_val_max: float
    y_val_min: float
    y_val_max: float
    dominant_outcome: str
    outcome_distribution: Dict[str, int]
    mean_stability: float
    mean_health: float
    mean_frag_duration_ms: float
    mean_retry_peak: float
    oscillation_count: int
    sample_count: int
    region_type: str   # stable | oscillation | frag_boundary | unstable | mixed


# ─────────────────────────────────────────────────────────────────────────────
# Batch Stability Mapper
# ─────────────────────────────────────────────────────────────────────────────

class BatchStabilityMapper:
    """
    Analyzes a completed batch and produces 2D stability maps for each
    topology across all parameter pairs.
    """

    # Parameter pairs to map (x_param, y_param)
    MAP_CONFIGS = [
        ("recovery_rate", "retry_backoff"),
        ("recovery_rate", "latency_multiplier"),
        ("recovery_rate", "node_capacity"),
        ("retry_backoff", "latency_multiplier"),
        ("retry_backoff", "node_capacity"),
        ("latency_multiplier", "node_capacity"),
    ]

    # Bins per axis (auto-computed from data if None)
    DEFAULT_N_BINS = 6

    def __init__(self, batch_dir: str):
        self.batch_dir = Path(batch_dir)
        self.results: List[dict] = []
        self.topologies: List[str] = []
        self.cell_grids: Dict[Tuple[str, str], Dict[Tuple[int, int], CellStats]] = {}
        self.boundary_points: List[dict] = []

    # ── Data loading ─────────────────────────────────────────────────────────

    def load_results(self) -> List[dict]:
        """Load all run results from a batch directory.

        Prefer comparative_results_augmented.csv if available (contains taxonomy
        fields added by run_classification), otherwise fall back to the base
        comparative_results.csv written by the Monte Carlo runner.
        """
        aug_path = self.batch_dir / "comparative_results_augmented.csv"
        csv_path = self.batch_dir / "comparative_results.csv"
        use_path = aug_path if aug_path.exists() else csv_path

        if not use_path.exists():
            raise FileNotFoundError(
                f"Neither comparative_results_augmented.csv nor "
                f"comparative_results.csv found in {self.batch_dir}"
            )

        with open(use_path) as f:
            reader = csv.DictReader(f)
            self.results = list(reader)

        # Parse numeric fields (they come as strings from CSV)
        for r in self.results:
            for field_ in ["final_health", "final_stability", "peak_p95_latency_ms",
                           "peak_retry_count", "fragmentation_duration_ms",
                           "recovery_rate", "retry_backoff", "node_capacity",
                           "latency_multiplier"]:
                if field_ in r and r[field_] not in ("", None):
                    try:
                        r[field_] = float(r[field_])
                    except (ValueError, TypeError):
                        r[field_] = None

        self.topologies = sorted(set(r.get("topology", "unknown") for r in self.results))
        return self.results

    # ── 2D binning ──────────────────────────────────────────────────────────

    def _compute_bins(self, values: List[float], n_bins: int = 6) -> List[float]:
        """Compute quantile-based bin edges."""
        if not values:
            return []
        sorted_vals = sorted(set(v for v in values if v is not None))
        if len(sorted_vals) <= n_bins:
            return sorted_vals
        step = len(sorted_vals) // n_bins
        bins = []
        for i in range(n_bins):
            idx = min(i * step, len(sorted_vals) - 1)
            bins.append(sorted_vals[idx])
        bins.append(sorted_vals[-1])
        return sorted(set(bins))

    def _find_bin(self, value: float, bins: List[float]) -> Optional[int]:
        if not bins or value < bins[0] or value > bins[-1]:
            return None
        for i in range(len(bins) - 1):
            if bins[i] <= value < bins[i + 1]:
                return i
        return len(bins) - 2 if bins else None

    def _classify_cell(self, cell_results: List[dict]) -> str:
        """Classify a parameter cell by dominant outcome."""
        if not cell_results:
            return "mixed"
        outcome_counts: Dict[str, int] = {}
        for r in cell_results:
            o = r.get("outcome", "unknown")
            if o:
                outcome_counts[o] = outcome_counts.get(o, 0) + 1
        if not outcome_counts:
            return "mixed"
        dominant = max(outcome_counts, key=outcome_counts.get)
        recovery_outcomes = ["full_recovery", "partial_recovery"]
        if dominant in recovery_outcomes:
            return "stable"
        if dominant == "oscillatory_instability":
            return "oscillation"
        if dominant in ("cascading_fragmentation",):
            return "frag_boundary"
        if dominant in ("secondary_collapse", "unrecoverable_partition"):
            return "unstable"
        return "mixed"

    def compute_maps(
        self,
        x_param: str,
        y_param: str,
        n_bins: int = 6,
        by_topology: bool = True,
    ) -> Dict[Tuple[int, int], CellStats]:
        """
        Compute a 2D stability map for (x_param, y_param).

        Returns:
            Dict mapping (x_bin_idx, y_bin_idx) -> CellStats
        """
        # Collect values for bin computation
        x_vals = [r.get(x_param) for r in self.results if r.get(x_param) is not None]
        y_vals = [r.get(y_param) for r in self.results if r.get(y_param) is not None]

        if not x_vals or not y_vals:
            return {}

        x_bins = self._compute_bins(x_vals, n_bins)
        y_bins = self._compute_bins(y_vals, n_bins)

        if len(x_bins) < 2 or len(y_bins) < 2:
            return {}

        # Group results by topology or global
        groups: Dict[Optional[str], List[dict]] = {}
        if by_topology and "topology" in self.results[0]:
            for r in self.results:
                topo = r.get("topology")
                groups.setdefault(topo, []).append(r)
        else:
            groups[None] = self.results

        all_cells: Dict[Tuple[int, int], CellStats] = {}

        for group_topo, group_results in groups.items():
            # Bin results
            cells: Dict[Tuple[int, int], List[dict]] = {}
            for r in group_results:
                x_val = r.get(x_param)
                y_val = r.get(y_param)
                if x_val is None or y_val is None:
                    continue
                x_bin = self._find_bin(x_val, x_bins)
                y_bin = self._find_bin(y_val, y_bins)
                if x_bin is not None and y_bin is not None:
                    cells.setdefault((x_bin, y_bin), []).append(r)

            # Compute cell statistics
            for (x_bin, y_bin), cell_results in cells.items():
                if len(cell_results) < 1:
                    continue

                # Bin centers and ranges
                x_center = (x_bins[x_bin] + x_bins[x_bin + 1]) / 2
                y_center = (y_bins[y_bin] + y_bins[y_bin + 1]) / 2

                # Outcome distribution
                outcome_counts: Dict[str, int] = {}
                for r in cell_results:
                    o = r.get("outcome", "unknown")
                    if o:
                        outcome_counts[o] = outcome_counts.get(o, 0) + 1

                # Metrics
                stabilities = [r["final_stability"] for r in cell_results
                               if r.get("final_stability") is not None]
                healths = [r["final_health"] for r in cell_results
                           if r.get("final_health") is not None]
                frag_durations = [r["fragmentation_duration_ms"] for r in cell_results
                                  if r.get("fragmentation_duration_ms") is not None]
                retry_peaks = [r["peak_retry_count"] for r in cell_results
                              if r.get("peak_retry_count") is not None]

                region_type = self._classify_cell(cell_results)
                dominant = max(outcome_counts, key=outcome_counts.get) if outcome_counts else "unknown"

                stats = CellStats(
                    x_bin_center=x_center,
                    y_bin_center=y_center,
                    x_val_min=x_bins[x_bin],
                    x_val_max=x_bins[x_bin + 1],
                    y_val_min=y_bins[y_bin],
                    y_val_max=y_bins[y_bin + 1],
                    dominant_outcome=dominant,
                    outcome_distribution=outcome_counts,
                    mean_stability=statistics.mean(stabilities) if stabilities else 0.0,
                    mean_health=statistics.mean(healths) if healths else 0.0,
                    mean_frag_duration_ms=statistics.mean(frag_durations) if frag_durations else 0.0,
                    mean_retry_peak=statistics.mean(retry_peaks) if retry_peaks else 0.0,
                    oscillation_count=sum(1 for r in cell_results
                                          if r.get("outcome") == "oscillatory_instability"),
                    sample_count=len(cell_results),
                    region_type=region_type,
                )

                # Include topology in key if grouped
                key = (group_topo or "", x_bin, y_bin) if by_topology else (x_bin, y_bin)
                all_cells[key] = stats

        self.cell_grids[(x_param, y_param)] = all_cells
        self._extract_boundaries(all_cells, x_param, y_param, by_topology)
        return all_cells

    def _extract_boundaries(
        self,
        cells: Dict,
        x_param: str,
        y_param: str,
        by_topology: bool,
    ):
        """Extract boundary points between region types."""
        for key, cell in cells.items():
            if cell.region_type not in REGION_TYPES:
                continue

            topo = key[0] if by_topology else None
            x_bin = key[-2] if by_topology else key[0]
            y_bin = key[-1] if by_topology else key[1]

            for dx, dy in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
                adj_key = (topo, x_bin + dx, y_bin + dy) if by_topology else (x_bin + dx, y_bin + dy)
                if adj_key in cells:
                    adj_cell = cells[adj_key]
                    if adj_cell.region_type != cell.region_type:
                        self.boundary_points.append({
                            "x_param": x_param,
                            "y_param": y_param,
                            "topology": topo or "global",
                            "x_value": cell.x_bin_center,
                            "y_value": cell.y_bin_center,
                            "region_type": cell.region_type,
                            "adjacent_region_type": adj_cell.region_type,
                            "sample_count": cell.sample_count,
                            "mean_stability": round(cell.mean_stability, 4),
                            "mean_health": round(cell.mean_health, 4),
                            "dominant_outcome": cell.dominant_outcome,
                        })

    def compute_all_maps(self, n_bins: int = 6) -> Dict[Tuple[str, str], Dict]:
        """Compute all configured parameter pair maps."""
        results = {}
        for (x_param, y_param) in self.MAP_CONFIGS:
            try:
                grid = self.compute_maps(x_param, y_param, n_bins=n_bins)
                results[(x_param, y_param)] = grid
            except Exception as e:
                print(f"[stability_mapper] Skipping {x_param} vs {y_param}: {e}")
        return results

    # ── Export ───────────────────────────────────────────────────────────────

    def export_all(
        self,
        output_dir: Optional[str] = None,
        batch_id: Optional[str] = None,
    ):
        """
        Export all stability maps and boundary data to disk.

        Creates:
          stability_maps/<x_param>_vs_<y_param>.csv
          resilience_frontiers/<topology>_frontier.csv
          phase_boundary_data.csv
        """
        if output_dir is None:
            output_dir = str(self.batch_dir)
        batch_id = batch_id or self.batch_dir.name

        maps_dir = os.path.join(output_dir, "stability_maps")
        frontiers_dir = os.path.join(output_dir, "resilience_frontiers")
        os.makedirs(maps_dir, exist_ok=True)
        os.makedirs(frontiers_dir, exist_ok=True)

        # Export each map
        for (x_param, y_param), cells in self.cell_grids.items():
            self._export_heatmap_csv(cells, x_param, y_param, maps_dir)

        # Export phase boundary data
        self._export_boundary_csv(frontiers_dir, batch_id)

        # Export resilience frontiers per topology
        self._export_frontier_csv(frontiers_dir)

        print(f"[stability_mapper] Exported to {output_dir}")
        print(f"  stability_maps/: {len(self.cell_grids)} maps")
        print(f"  boundary_points: {len(self.boundary_points)}")

    def _export_heatmap_csv(
        self,
        cells: Dict,
        x_param: str,
        y_param: str,
        output_dir: str,
    ):
        """Export a single heatmap as CSV (x_bin rows, y_bin cols)."""
        if not cells:
            return

        # Extract unique x_bins and y_bins
        all_keys = list(cells.keys())
        has_topo = len(all_keys[0]) == 3  # (topo, x_bin, y_bin)

        if has_topo:
            # Group by topology
            by_topo: Dict[str, List] = {}
            for key, cell in cells.items():
                topo = key[0]
                by_topo.setdefault(topo, []).append((key[1], key[2], cell))

            for topo, topo_cells in by_topo.items():
                self._write_heatmap(topo_cells, x_param, y_param, output_dir, topo)
        else:
            self._write_heatmap(all_keys, x_param, y_param, output_dir, None)

    def _write_heatmap(
        self,
        cells: List,
        x_param: str,
        y_param: str,
        output_dir: str,
        topo: Optional[str],
    ):
        """Write one heatmap CSV from list of (x_bin, y_bin, cell) tuples."""
        x_bins = sorted(set(c[0] for c in cells))
        y_bins = sorted(set(c[1] for c in cells))

        if not x_bins or not y_bins:
            return

        # Build matrix [y_bin][x_bin]
        matrix: Dict[int, Dict[int, float]] = {yb: {} for yb in y_bins}
        x_centers: Dict[int, float] = {}
        y_centers: Dict[int, float] = {}

        for x_bin, y_bin, cell in cells:
            matrix.setdefault(y_bin, {})[x_bin] = round(cell.mean_stability, 4)
            x_centers[x_bin] = round(cell.x_bin_center, 4)
            y_centers[y_bin] = round(cell.y_bin_center, 4)

        prefix = f"{topo}_" if topo else ""
        path = os.path.join(output_dir, f"{prefix}{x_param}_vs_{y_param}.csv")

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            # Header row: x bin centers
            x_header = [f"{x_param}_bin_center"] + [str(x_centers[xb]) for xb in x_bins]
            writer.writerow(x_header)
            # Data rows: y_bin_center followed by stability values
            for y_bin in y_bins:
                row = [str(y_centers[y_bin])] + [
                    str(matrix[y_bin].get(xb, "")) for xb in x_bins
                ]
                writer.writerow(row)

    def _export_boundary_csv(self, output_dir: str, batch_id: str):
        """Export all detected phase boundary points."""
        if not self.boundary_points:
            return
        path = os.path.join(output_dir, "phase_boundary_data.csv")
        fieldnames = list(self.boundary_points[0].keys())
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self.boundary_points)
        print(f"[stability_mapper] phase_boundary_data.csv: {len(self.boundary_points)} points")

    def _export_frontier_csv(self, output_dir: str):
        """Export resilience frontier: minimum y for stable cells at each x."""
        if not self.cell_grids:
            return

        for (x_param, y_param), cells in self.cell_grids.items():
            stable_cells = [
                (k, c) for k, c in cells.items()
                if c.region_type == "stable"
            ]
            if not stable_cells:
                continue

            # Find minimum y for stable at each x_bin
            frontier: Dict[int, Tuple[float, CellStats]] = {}
            for key, cell in stable_cells:
                x_bin = key[-2] if len(key) == 3 else key[0]
                if x_bin not in frontier or cell.y_bin_center < frontier[x_bin][0]:
                    frontier[x_bin] = (cell.y_bin_center, cell)

            rows = []
            for x_bin in sorted(frontier):
                y_center, cell = frontier[x_bin]
                rows.append({
                    "x_param": x_param,
                    "y_param": y_param,
                    "topology": key[0] if len(key) == 3 else "global",
                    "x_bin_center": round(cell.x_bin_center, 4),
                    "y_stability_min": round(y_center, 4),
                    "mean_stability": round(cell.mean_stability, 4),
                    "mean_health": round(cell.mean_health, 4),
                    "sample_count": cell.sample_count,
                })

            if rows:
                topo = rows[0]["topology"]
                prefix = f"{topo}_" if topo and topo != "global" else ""
                path = os.path.join(output_dir, f"{prefix}resilience_frontier_{x_param}_vs_{y_param}.csv")
                with open(path, "w", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                    writer.writeheader()
                    writer.writerows(rows)

    # ── Analysis helpers ─────────────────────────────────────────────────────

    def get_stability_summary(self) -> dict:
        """Produce a summary dict of the stability landscape."""
        summary = {
            "total_runs": len(self.results),
            "topologies": self.topologies,
            "parameter_pairs_mapped": list(self.cell_grids.keys()),
            "boundary_points_total": len(self.boundary_points),
            "region_type_counts": {},
            "topology_stability_ranking": {},
        }

        # Count region types across all cells
        region_counts: Dict[str, int] = {}
        for cells in self.cell_grids.values():
            for cell in cells.values():
                region_counts[cell.region_type] = region_counts.get(cell.region_type, 0) + 1
        summary["region_type_counts"] = region_counts

        # Mean stability per topology
        for topo in self.topologies:
            topo_results = [r for r in self.results if r.get("topology") == topo]
            stabilities = [r["final_stability"] for r in topo_results
                           if r.get("final_stability") is not None]
            if stabilities:
                summary["topology_stability_ranking"][topo] = {
                    "mean_stability": round(statistics.mean(stabilities), 4),
                    "std_stability": round(statistics.stdev(stabilities), 4) if len(stabilities) > 1 else 0,
                    "n": len(stabilities),
                }

        return summary


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Cascade Stability Landscape Mapper")
    parser.add_argument("batch_dir", help="Path to batch directory")
    parser.add_argument("--output-dir", default=None, help="Override output directory")
    parser.add_argument("--n-bins", type=int, default=6, help="Bins per axis (default: 6)")
    parser.add_argument("--no-topology", action="store_true", help="Compute global maps only")
    args = parser.parse_args()

    mapper = BatchStabilityMapper(args.batch_dir)
    mapper.load_results()
    print(f"[stability_mapper] Loaded {len(mapper.results)} runs, topologies={mapper.topologies}")

    mapper.compute_all_maps(n_bins=args.n_bins)
    print(f"[stability_mapper] Computed {len(mapper.cell_grids)} maps")

    mapper.export_all(output_dir=args.output_dir, batch_id=mapper.batch_dir.name)

    summary = mapper.get_stability_summary()
    print(f"[stability_mapper] Summary:")
    print(f"  region_type_counts: {summary['region_type_counts']}")
    print(f"  topology_stability_ranking:")
    for topo, stats in summary.get("topology_stability_ranking", {}).items():
        print(f"    {topo}: mean={stats['mean_stability']}, std={stats['std_stability']}, n={stats['n']}")


if __name__ == "__main__":
    main()
