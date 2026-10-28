"""Synthetic evaluation dataset: deterministic generator, strict CSV loader and evaluation.

No streamlit, no sqlite. The dataset is a CSV file with the columns
``sample_id, tracking_id, ground_truth, predicted_label`` where
1 = Ticketless passenger (positive class) and 0 = Valid-ticket passenger.

Every row is synthetic: ``sample_id`` and ``tracking_id`` start with ``SYN-``. The
ground truth is drawn first from a fixed-seed ``random.Random``; ``predicted_label``
is then simulated from it with a fixed true-positive and false-positive rate. No camera,
YOLO model or database record is involved, so metrics computed from this file only check
the metric calculation and are not real-world model accuracy.

The file lives at ``data/dummy_evaluation_dataset.csv`` unless the **process
environment** variable ``TPD_EVAL_DATASET`` points elsewhere (``.env`` is not read
for this variable). Metrics come only from :func:`app.metric_evaluation.confusion_matrix`
and :func:`app.metric_evaluation.calculate_metrics`.
"""

from __future__ import annotations

import csv
import io
import os
import random
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from app import metric_evaluation as me
from app.config import PROJECT_ROOT, resolve_path

COLUMNS = ("sample_id", "tracking_id", "ground_truth", "predicted_label")
DEFAULT_RELATIVE_PATH = "data/dummy_evaluation_dataset.csv"
ENV_VAR = "TPD_EVAL_DATASET"
DATASET_NAME = "Dummy evaluation dataset (synthetic)"
SYNTHETIC_PREFIX = "SYN-"
DEFAULT_SEED = 20260115  # same as scripts/generate_dummy_data.DEFAULT_SEED
DEFAULT_SAMPLES = 200
MIN_SAMPLES = 100
MAX_SAMPLES = 100_000
MAX_FILE_BYTES = 5_000_000
MAX_ID = 64
MAX_DETAILS = 5
POSITIVE_RATE = 0.40
SIMULATED_TPR = 0.85
SIMULATED_FPR = 0.10
METHOD_TEXT = "Confusion matrix of ground_truth vs predicted_label (app.metric_evaluation.confusion_matrix)"
MODEL_TEXT = "None – predicted_label is simulated by app.evaluation_dataset (no camera or YOLO model involved)"
LABEL_FIELDS = ("ground_truth", "predicted_label")
ID_FIELDS = ("sample_id", "tracking_id")


@dataclass(frozen=True)
class Sample:
    sample_id: str
    tracking_id: str
    ground_truth: int
    predicted_label: int


@dataclass(frozen=True)
class LoadedDataset:
    path: Path
    name: str
    samples: Tuple[Sample, ...]
    is_synthetic: bool
    matches_generator: bool
    modified_at: datetime


@dataclass(frozen=True)
class DatasetEvaluation:
    counts: me.EvaluationCounts
    result: me.MetricResult


class DatasetError(Exception):
    """A dataset file that cannot be used; ``details`` lists up to 5 row errors (plus "… and N more")."""

    def __init__(self, message: str, details: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.details: Tuple[str, ...] = tuple(details)


# ------------------------------------------------------------------ paths
def dataset_path() -> Path:
    """``$TPD_EVAL_DATASET`` (process environment only) or ``data/dummy_evaluation_dataset.csv``."""
    value = os.environ.get(ENV_VAR, "").strip()
    return resolve_path(value) if value else resolve_path(DEFAULT_RELATIVE_PATH)


def display_path(path: Path) -> str:
    """Project-relative POSIX path, or only the file name for paths outside the project."""
    try:
        return Path(path).resolve().relative_to(PROJECT_ROOT).as_posix()
    except (ValueError, OSError):
        return Path(path).name


# ------------------------------------------------------------------ generator
def generate_samples(seed: int = DEFAULT_SEED, n: int = DEFAULT_SAMPLES) -> Tuple[Sample, ...]:
    """Deterministic synthetic samples for ``(seed, n)``; the ground truth is drawn before the prediction."""
    if isinstance(n, bool) or not isinstance(n, int) or not MIN_SAMPLES <= n <= MAX_SAMPLES:
        raise ValueError(f"samples must be an integer from {MIN_SAMPLES} to {MAX_SAMPLES}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    rng = random.Random(seed)
    samples: List[Sample] = []
    for i in range(1, n + 1):
        gt = 1 if rng.random() < POSITIVE_RATE else 0
        r = rng.random()
        pred = int(r < SIMULATED_TPR) if gt == 1 else int(r < SIMULATED_FPR)
        samples.append(Sample(f"{SYNTHETIC_PREFIX}{i:04d}", f"{SYNTHETIC_PREFIX}TRK-{i:04d}", gt, pred))
    return tuple(samples)


def to_csv_text(samples: Iterable[Sample]) -> str:
    """Header + one row per sample with LF line endings (byte-identical on every OS)."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    for s in samples:
        writer.writerow((s.sample_id, s.tracking_id, s.ground_truth, s.predicted_label))
    return buffer.getvalue()


# ------------------------------------------------------------------ writer
def _reason(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


def is_replaceable(path: Path) -> Tuple[bool, str]:
    """``(True, "")`` if ``path`` is absent or holds only synthetic rows, else ``(False, reason)``.

    Line-based on purpose (no ``csv``): a malformed file whose rows all start with
    ``SYN-`` can be regenerated, while any row that is not marked synthetic blocks it.
    Unknown or unreadable content is never overwritten.
    """
    path = Path(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        return True, ""
    except OSError as exc:
        return False, f"it could not be read ({_reason(exc)})"
    if not stat.S_ISREG(info.st_mode):
        return False, "it is not a regular file"
    if info.st_size > MAX_FILE_BYTES:
        return False, "it is larger than 5 MB"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return True, ""
    except OSError as exc:
        return False, f"it could not be read ({_reason(exc)})"
    if len(raw) > MAX_FILE_BYTES:
        return False, "it is larger than 5 MB"
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False, "it is not UTF-8 text"
    for line in text.splitlines()[1:]:  # first line = header, any content
        if not line.strip():
            continue
        first = line.split(",", 1)[0].strip()
        if len(first) >= 2 and first.startswith('"') and first.endswith('"'):
            first = first[1:-1]
        if not first.startswith(SYNTHETIC_PREFIX):
            return False, "it contains rows that are not marked synthetic (SYN- prefix)"
    return True, ""


def write_dataset(path: Path, samples: Sequence[Sample], *, overwrite_non_synthetic: bool = False) -> Path:
    """Atomically write ``samples`` to ``path`` (temp file + ``os.replace``).

    Refuses (``DatasetError``, nothing written) when the existing file is not a
    synthetic dataset, unless ``overwrite_non_synthetic`` (the CLI's ``--force``; the
    dashboard never passes it). The window between the check and ``os.replace`` is
    accepted: the file is local and single-user. ``OSError`` propagates; the old file
    then stays as it was.
    """
    path = Path(path)
    if not overwrite_non_synthetic:
        ok, reason = is_replaceable(path)
        if not ok:
            raise DatasetError(f"Refusing to overwrite {display_path(path)}: {reason}. Only synthetic dataset "
                               "files (SYN- rows) are regenerated.", ())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(to_csv_text(samples).encode("utf-8"))
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


# ------------------------------------------------------------------ loader
def _is_blank(row: List[str]) -> bool:
    return not row or (len(row) == 1 and not row[0].strip())


def _details(errors: List[str]) -> Tuple[str, ...]:
    shown = errors[:MAX_DETAILS]
    if len(errors) > MAX_DETAILS:
        shown.append(f"… and {len(errors) - MAX_DETAILS} more")
    return tuple(shown)


def _short(value: str) -> str:
    return value if len(value) <= 20 else value[:20] + "…"


def load_dataset(path: Optional[Path] = None) -> LoadedDataset:
    """Strictly parse a dataset file (fail-closed); the only exception raised is ``DatasetError``."""
    path = dataset_path() if path is None else Path(path)
    shown = display_path(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        raise DatasetError(f"Dataset file not found: {shown}", ()) from None
    except OSError as exc:
        raise DatasetError(f"Dataset file could not be read: {shown} ({_reason(exc)})", ()) from None
    if not stat.S_ISREG(info.st_mode):
        raise DatasetError(f"Dataset file not found: {shown}", ())  # e.g. a directory
    if info.st_size > MAX_FILE_BYTES:
        raise DatasetError("Dataset file is larger than 5 MB", ())
    try:
        raw = path.read_bytes()
    except FileNotFoundError:  # deleted between stat and read
        raise DatasetError(f"Dataset file not found: {shown}", ()) from None
    except OSError as exc:  # PermissionError (open in Excel), sharing violation
        raise DatasetError(f"Dataset file could not be read: {shown} ({_reason(exc)})", ()) from None
    if len(raw) > MAX_FILE_BYTES:  # grew after stat
        raise DatasetError("Dataset file is larger than 5 MB", ())
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise DatasetError("Dataset file is not valid UTF-8 text", ()) from None
    if "\x00" in text:  # Python >= 3.11's csv accepts NUL; a text dataset never contains one
        raise DatasetError("Dataset file is not valid CSV: line contains NUL", ())
    try:
        reader = csv.reader(io.StringIO(text, newline=""))
        rows = [(reader.line_num, row) for row in reader]
    except csv.Error as exc:  # "line contains NUL", field larger than field limit
        raise DatasetError(f"Dataset file is not valid CSV: {exc}", ()) from None

    rows = [(n, row) for n, row in rows if not _is_blank(row)]
    if not rows:
        raise DatasetError("Dataset file is empty", ())
    header = [name.strip() for name in rows[0][1]]
    seen = set()
    for name in header:
        if name in seen:
            raise DatasetError(f"Duplicate column: {name}", ())
        seen.add(name)
    if len(header) != len(COLUMNS) or set(header) != set(COLUMNS):
        raise DatasetError(f"Expected columns {', '.join(COLUMNS)}; found {', '.join(header) or 'none'}", ())
    body = rows[1:]
    if not body:
        raise DatasetError("Dataset has no samples", ())
    if len(body) > MAX_SAMPLES:
        raise DatasetError(f"Dataset has more than {MAX_SAMPLES} samples", ())

    index = {name: i for i, name in enumerate(header)}
    errors: List[str] = []
    samples: List[Sample] = []
    ids = set()
    for line, row in body:
        if len(row) != len(header):
            errors.append(f"line {line}: expected {len(header)} values, found {len(row)}")
            continue
        cells = {name: row[i].strip() for name, i in index.items()}
        row_errors = []
        for name in ID_FIELDS:
            value = cells[name]
            if not value:
                row_errors.append(f"line {line}: {name} is missing")
            elif len(value) > MAX_ID:
                row_errors.append(f"line {line}: {name} is longer than {MAX_ID} characters")
        sample_id = cells["sample_id"]
        if sample_id and sample_id in ids:
            row_errors.append(f"line {line}: duplicate sample_id {_short(sample_id)}")
        ids.add(sample_id)
        for name in LABEL_FIELDS:
            value = cells[name]
            if value == "":
                row_errors.append(f"line {line}: {name} is missing")
            elif value not in ("0", "1"):
                row_errors.append(f"line {line}: {name} is invalid ({_short(value)}); expected 0 or 1")
        if row_errors:
            errors.extend(row_errors)
            continue
        samples.append(Sample(sample_id, cells["tracking_id"], int(cells["ground_truth"]),
                              int(cells["predicted_label"])))
    if errors:
        raise DatasetError(f"Dataset has {len(errors)} invalid value(s)", _details(errors))

    is_synthetic = all(s.sample_id.startswith(SYNTHETIC_PREFIX) and s.tracking_id.startswith(SYNTHETIC_PREFIX)
                       for s in samples)
    if not is_synthetic:
        raise DatasetError("Dataset rows are not marked synthetic (SYN- prefix). This mode only evaluates the "
                           "synthetic dummy dataset.", ())
    loaded = tuple(samples)
    matches = MIN_SAMPLES <= len(loaded) <= MAX_SAMPLES and loaded == generate_samples(DEFAULT_SEED, len(loaded))
    return LoadedDataset(path=path, name=DATASET_NAME, samples=loaded, is_synthetic=True,
                         matches_generator=bool(matches),
                         modified_at=datetime.fromtimestamp(info.st_mtime, timezone.utc))


# ------------------------------------------------------------------ evaluation
def evaluate(dataset: LoadedDataset) -> DatasetEvaluation:
    """Confusion matrix and the four metrics, via the single metric location (``app.metric_evaluation``)."""
    counts = me.confusion_matrix((s.ground_truth, s.predicted_label) for s in dataset.samples)
    return DatasetEvaluation(counts, me.calculate_metrics(counts.tp, counts.tn, counts.fp, counts.fn))
