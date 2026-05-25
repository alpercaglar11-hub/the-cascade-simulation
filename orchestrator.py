#!/usr/bin/env python3
"""
orchestrator.py — Cascade Experiment Pipeline Orchestrator
==========================================================
Single entry point that ties the experiment pipeline together:

  1. Registers a batch directory with the experiment registry
  2. Generates a Markdown research report from registered data
  3. Writes the report to reports/latest_experiment.md

All paths are absolute and resolved from this file's location,
so the script works regardless of the current working directory.

Usage:
  python orchestrator.py <experiments_folder>
  python orchestrator.py /absolute/path/to/experiments/batch_abc123

Examples:
  # From cascade root:
  python orchestrator.py experiments/experiment_01

  # From anywhere:
  python orchestrator.py /home/alper/videolar/.../the_cascade/experiments/experiment_01

  # Auto-discover latest experiment folder:
  python orchestrator.py --latest

Environment variables:
  CASCADE_ROOT   Override the cascade root directory detection.
                 Defaults to the parent of this script's directory.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ── Resolve cascade root from script location ─────────────────────────────────
# This file lives at: <cascade_root>/orchestrator.py
# Its parent is the cascade root directory.
_SCRIPT_PATH = Path(__file__).resolve()
CASCADE_ROOT = os.environ.get("CASCADE_ROOT", str(_SCRIPT_PATH.parent))

# Ensure the cascade root is on sys.path so we can import local packages
sys.path.insert(0, CASCADE_ROOT)

from metrics.experiment_registry.experiment_registry import ExperimentRegistry
from reports.generate_report import generate_report, load_latest_batch

REGISTRY_ROOT = os.path.join(CASCADE_ROOT, "metrics", "experiment_registry")
REPORTS_DIR = os.path.join(CASCADE_ROOT, "reports")
DEFAULT_OUTPUT = os.path.join(REPORTS_DIR, "latest_experiment.md")


# ── Helpers ──────────────────────────────────────────────────────────────────

def resolve_batch_dir(experiments_folder: str) -> Path:
    """Resolve experiments_folder to an absolute Path."""
    p = Path(experiments_folder).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Path does not exist: {p}")
    return p


def discover_latest_experiment_folder() -> Path:
    """Find the most recently modified batch_* directory under experiments/."""
    experiments_dir = Path(CASCADE_ROOT) / "experiments"
    if not experiments_dir.exists():
        raise FileNotFoundError(
            f"No experiments/ directory found at: {experiments_dir}\n"
            f"Hint: run a Monte Carlo sweep first: python experiments/monte_carlo_runner.py"
        )

    batch_dirs = sorted(experiments_dir.glob("batch_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not batch_dirs:
        raise FileNotFoundError(
            f"No batch_* directories found in: {experiments_dir}\n"
            f"Hint: run a Monte Carlo sweep first to generate experiment batches."
        )

    return batch_dirs[0]


def print_step(step: str, msg: str):
    """Print a labelled pipeline step."""
    print(f"\n[orchestrator] {step:20s}  {msg}")


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run(experiments_folder: str, output_path: str = DEFAULT_OUTPUT) -> str:
    """
    Execute the full registration + report pipeline.

    Args:
        experiments_folder:  Path to a batch directory containing
                            batch_metadata.json and aggregate_summary.json.
                            Use "--latest" to auto-discover the most recent batch.
        output_path:        Where to write the Markdown report.
                            Defaults to reports/latest_experiment.md.

    Returns:
        The path to the generated report file.
    """
    # ── Step 1: Locate the batch directory ──────────────────────────────────
    if experiments_folder == "--latest":
        batch_dir = discover_latest_experiment_folder()
    else:
        batch_dir = resolve_batch_dir(experiments_folder)

    print_step("batch_dir", str(batch_dir))

    # ── Step 2: Register with experiment registry ───────────────────────────
    registry = ExperimentRegistry(REGISTRY_ROOT)
    try:
        entry = registry.register_batch_from_path(str(batch_dir))
    except FileNotFoundError as exc:
        sys.stderr.write(f"\n[orchestrator] ERROR: {exc}\n")
        sys.stderr.write(
            f"\nExpected contents:\n"
            f"  {batch_dir}/batch_metadata.json\n"
            f"  {batch_dir}/aggregate_summary.json\n"
            f"  optionally: {batch_dir}/comparative_results.csv\n"
            f"  optionally: {batch_dir}/config_snapshots/\n\n"
        )
        sys.exit(1)
    except ValueError as exc:
        sys.stderr.write(f"\n[orchestrator] SCHEMA VALIDATION ERROR: {exc}\n")
        sys.exit(1)

    batch_id = entry["batch_id"]
    print_step("registered", f"batch_id={batch_id[:8]} recovery_success_rate={entry.get('recovery_success_rate', 'N/A')}")

    # ── Step 3: Generate markdown report ────────────────────────────────────
    report = generate_report(registry, [batch_id], output_path=output_path)
    report_lines = report.split("\n")
    section_headers = [l.strip() for l in report_lines if l.startswith("## ")]
    print_step("report sections", f"{len(section_headers)} — {' | '.join(section_headers)}")

    # ── Done ─────────────────────────────────────────────────────────────────
    abs_output = Path(output_path).resolve()
    print_step("output written", str(abs_output))

    return str(abs_output)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Cascade experiment pipeline: register a batch and generate a research report.",
        epilog=(
            "Examples:\n"
            "  python orchestrator.py experiments/experiment_01\n"
            "  python orchestrator.py --latest\n"
            "  python orchestrator.py /full/path/to/batch_abc123 --output reports/my_report.md\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "experiments_folder",
        nargs="?",
        default=None,
        help="Path to a batch_* directory. Use --latest to auto-discover the most recent batch.",
    )
    parser.add_argument(
        "--output", "-o",
        default=DEFAULT_OUTPUT,
        help=f"Output .md file path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Auto-discover and use the most recent batch_* directory in experiments/",
    )

    args = parser.parse_args()

    if args.latest:
        folder = str(discover_latest_experiment_folder())
        print(f"[orchestrator] Auto-discovered latest batch: {folder}")
    elif args.experiments_folder:
        folder = args.experiments_folder
    else:
        parser.print_help()
        sys.exit(0)

    if not Path(folder).exists():
        sys.stderr.write(f"[orchestrator] ERROR: path does not exist: {folder}\n")
        sys.exit(1)

    output_path = args.output
    if output_path == DEFAULT_OUTPUT:
        # Ensure reports/ directory exists before we potentially write into it
        Path(REPORTS_DIR).mkdir(parents=True, exist_ok=True)

    result = run(experiments_folder=folder, output_path=output_path)
    print(f"\n[orchestrator] DONE — report at: {result}")