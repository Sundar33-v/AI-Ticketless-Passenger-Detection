"""Write the synthetic evaluation dataset (fixed seed) and print its confusion matrix.

    python scripts/generate_evaluation_dataset.py [--seed 20260115] [--samples 200] [--output PATH] [--force]

Columns: sample_id, tracking_id, ground_truth, predicted_label
(1 = Ticketless passenger, 0 = Valid-ticket passenger). Every row is synthetic (SYN- IDs).
The default output is ``TPD_EVAL_DATASET`` (process environment) or
``data/dummy_evaluation_dataset.csv``. An existing file that is not a synthetic dataset
is never overwritten unless ``--force`` is given.
Exit codes: 0 ok, 1 usage or invalid value, 2 file error or refused overwrite.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Sequence

import _bootstrap  # noqa: F401  (puts the project root on sys.path)

from app.config import resolve_path
from app.evaluation_dataset import (DEFAULT_SAMPLES, DEFAULT_SEED, MAX_SAMPLES, MIN_SAMPLES, DatasetError,
                                    dataset_path, evaluate, generate_samples, load_dataset, write_dataset)

SYNTHETIC_NOTE = "Synthetic evaluation data – not real passenger records."


def build_parser() -> _bootstrap.ArgumentParser:
    parser = _bootstrap.ArgumentParser(description="Generate the synthetic (dummy) evaluation dataset.")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"random seed (default {DEFAULT_SEED})")
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES,
                        help=f"number of samples, {MIN_SAMPLES}..{MAX_SAMPLES} (default {DEFAULT_SAMPLES})")
    parser.add_argument("--output", help="CSV file (default: TPD_EVAL_DATASET or data/dummy_evaluation_dataset.csv)")
    parser.add_argument("--force", action="store_true",
                        help="overwrite the output even if it is not a synthetic dataset file")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    path: Path = resolve_path(args.output) if args.output else dataset_path()
    try:
        samples = generate_samples(args.seed, args.samples)
    except ValueError as exc:
        print(f"Invalid value: {exc}", file=sys.stderr)
        return 1
    try:
        write_dataset(path, samples, overwrite_non_synthetic=args.force)
    except DatasetError as exc:
        print(exc.message, file=sys.stderr)
        print("Use --force to overwrite it anyway.", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Could not write {path}: {exc.strerror or exc}", file=sys.stderr)
        return 2
    try:
        evaluation = evaluate(load_dataset(path))
    except DatasetError as exc:
        print(exc.message, file=sys.stderr)
        for detail in exc.details:
            print(f"  {detail}", file=sys.stderr)
        return 2
    c = evaluation.counts
    print(f"Dataset: {path}")
    print(f"Samples: {c.total} (seed {args.seed})")
    print(f"TP {c.tp} · TN {c.tn} · FP {c.fp} · FN {c.fn}")
    print(SYNTHETIC_NOTE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
