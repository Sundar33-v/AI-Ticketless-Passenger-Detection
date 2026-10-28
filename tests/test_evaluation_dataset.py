"""Tests for app/evaluation_dataset.py, scripts/generate_evaluation_dataset.py and the committed dummy dataset."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import generate_dummy_data
import generate_evaluation_dataset as cli
from app import evaluation_dataset as ds
from app import metric_evaluation as me
from app.config import PROJECT_ROOT

ROOT = Path(__file__).resolve().parents[1]
COMMITTED = ROOT / "data" / "dummy_evaluation_dataset.csv"
HEADER = "sample_id,tracking_id,ground_truth,predicted_label\n"
REAL_ROWS = (HEADER + "REAL-001,42,1,0\n").encode()


def write(path, text):
    path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    return path


def error_of(path):
    with pytest.raises(ds.DatasetError) as info:  # the loader raises nothing but DatasetError
        ds.load_dataset(path)
    return info.value


# ------------------------------------------------------------------ generator
def test_generator_is_reproducible_and_lf_only():
    first, second = ds.generate_samples(), ds.generate_samples()
    assert first == second and len(first) == ds.DEFAULT_SAMPLES == 200
    text = ds.to_csv_text(first)
    assert text == ds.to_csv_text(second)
    assert "\r" not in text and text.startswith(HEADER)
    assert len(text.splitlines()) == 201


def test_different_seed_gives_different_samples():
    assert ds.generate_samples(1) != ds.generate_samples(2)
    assert ds.generate_samples(7) == ds.generate_samples(7)


def test_default_dataset_shape():
    samples = ds.generate_samples()
    assert len(samples) >= 100
    assert all(s.sample_id.startswith("SYN-") and s.tracking_id.startswith("SYN-") for s in samples)
    assert len({s.sample_id for s in samples}) == len(samples)
    outcomes = {(s.ground_truth, s.predicted_label) for s in samples}
    assert outcomes == {(1, 1), (0, 0), (0, 1), (1, 0)}  # TP, TN, FP and FN all present
    assert all(s.ground_truth in (0, 1) and s.predicted_label in (0, 1) for s in samples)


def test_ground_truth_does_not_depend_on_predictions(monkeypatch):
    truth = [s.ground_truth for s in ds.generate_samples()]
    predicted = [s.predicted_label for s in ds.generate_samples()]
    monkeypatch.setattr(ds, "SIMULATED_TPR", 0.0)
    monkeypatch.setattr(ds, "SIMULATED_FPR", 1.0)
    changed = ds.generate_samples()
    assert [s.ground_truth for s in changed] == truth
    assert [s.predicted_label for s in changed] != predicted


@pytest.mark.parametrize("n", [99, 100_001, True, 150.0])
def test_generator_rejects_bad_sample_counts(n):
    with pytest.raises(ValueError):
        ds.generate_samples(n=n)


def test_seed_matches_demo_data_seed():
    assert ds.DEFAULT_SEED == generate_dummy_data.DEFAULT_SEED == 20260115


def test_evaluate_uses_confusion_matrix_and_sums_to_total(tmp_path):
    path = ds.write_dataset(tmp_path / "d.csv", ds.generate_samples())
    loaded = ds.load_dataset(path)
    result = ds.evaluate(loaded)
    c = result.counts
    assert c.tp + c.tn + c.fp + c.fn == len(loaded.samples) == 200
    assert c == me.confusion_matrix((s.ground_truth, s.predicted_label) for s in loaded.samples)
    assert result.result == me.calculate_metrics(c.tp, c.tn, c.fp, c.fn)


# ------------------------------------------------------------------ loader
def test_round_trip_and_metadata(tmp_path):
    path = ds.write_dataset(tmp_path / "d.csv", ds.generate_samples())
    loaded = ds.load_dataset(path)
    assert loaded.samples == ds.generate_samples()
    assert loaded.is_synthetic and loaded.matches_generator and loaded.name == ds.DATASET_NAME
    assert loaded.modified_at.tzinfo is not None


def test_bom_crlf_and_blank_lines_accepted(tmp_path):
    text = ds.to_csv_text(ds.generate_samples()).replace("\n", "\r\n") + "\r\n\r\n"
    path = write(tmp_path / "d.csv", b"\xef\xbb\xbf" + text.encode())
    loaded = ds.load_dataset(path)
    assert loaded.samples == ds.generate_samples() and loaded.matches_generator


def test_columns_in_any_order(tmp_path):
    path = write(tmp_path / "d.csv", "predicted_label,ground_truth,tracking_id,sample_id\n1,0,SYN-T1,SYN-1\n")
    loaded = ds.load_dataset(path)
    assert loaded.samples == (ds.Sample("SYN-1", "SYN-T1", 0, 1),)
    assert loaded.matches_generator is False  # fewer than 100 rows never "matches"


def test_missing_file_and_directory(tmp_path):
    assert error_of(tmp_path / "nope.csv").message == "Dataset file not found: nope.csv"
    folder = tmp_path / "folder.csv"
    folder.mkdir()
    assert error_of(folder).message == "Dataset file not found: folder.csv"


@pytest.mark.parametrize("content, message", [
    ("", "Dataset file is empty"),
    ("\n\n", "Dataset file is empty"),
    (HEADER, "Dataset has no samples"),
    ("sample_id,tracking_id,ground_truth\nSYN-1,SYN-T1,1\n",
     "Expected columns sample_id, tracking_id, ground_truth, predicted_label; found sample_id, tracking_id, "
     "ground_truth"),
    ("sample_id,tracking_id,ground_truth,predicted_label,extra\nSYN-1,SYN-T1,1,1,x\n",
     "Expected columns sample_id, tracking_id, ground_truth, predicted_label; found sample_id, tracking_id, "
     "ground_truth, predicted_label, extra"),
    ("sample_id,sample_id,ground_truth,predicted_label\nSYN-1,SYN-T1,1,1\n", "Duplicate column: sample_id"),
    (HEADER + "REAL-001,42,1,0\n",
     "Dataset rows are not marked synthetic (SYN- prefix). This mode only evaluates the synthetic dummy dataset."),
])
def test_loader_structure_errors(tmp_path, content, message):
    assert error_of(write(tmp_path / "d.csv", content)).message == message


@pytest.mark.parametrize("row, detail", [
    ("SYN-1,SYN-T1,,1", "line 2: ground_truth is missing"),
    ("SYN-1,SYN-T1,1,", "line 2: predicted_label is missing"),
    ("SYN-1,SYN-T1,2,1", "line 2: ground_truth is invalid (2); expected 0 or 1"),
    ("SYN-1,SYN-T1,1,-1", "line 2: predicted_label is invalid (-1); expected 0 or 1"),
    ("SYN-1,SYN-T1,yes,1", "line 2: ground_truth is invalid (yes); expected 0 or 1"),
    ("SYN-1,SYN-T1,1.0,1", "line 2: ground_truth is invalid (1.0); expected 0 or 1"),
    (",SYN-T1,1,1", "line 2: sample_id is missing"),
    ("SYN-1,,1,1", "line 2: tracking_id is missing"),
    ("SYN-1,SYN-T1,1", "line 2: expected 4 values, found 3"),
    ("SYN-" + "x" * 61 + ",SYN-T1,1,1", "line 2: sample_id is longer than 64 characters"),
])
def test_loader_row_errors(tmp_path, row, detail):
    error = error_of(write(tmp_path / "d.csv", HEADER + row + "\n"))
    assert error.message.startswith("Dataset has 1 invalid value")
    assert error.details == (detail,)


def test_duplicate_sample_id(tmp_path):
    error = error_of(write(tmp_path / "d.csv", HEADER + "SYN-1,SYN-T1,1,1\nSYN-1,SYN-T2,0,0\n"))
    assert error.details == ("line 3: duplicate sample_id SYN-1",)


def test_details_are_capped(tmp_path):
    rows = "".join(f"SYN-{i},SYN-T{i},9,1\n" for i in range(8))
    error = error_of(write(tmp_path / "d.csv", HEADER + rows))
    assert len(error.details) == 6 and error.details[-1] == "… and 3 more"


def test_invalid_utf8_nul_and_size(tmp_path, monkeypatch):
    assert error_of(write(tmp_path / "a.csv", HEADER.encode() + b"SYN-1,SYN-T1,\xff,1\n")).message == \
        "Dataset file is not valid UTF-8 text"
    assert error_of(write(tmp_path / "b.csv", HEADER + "SYN-1,SYN-T1,1,\x001\n")).message.startswith(
        "Dataset file is not valid CSV")
    monkeypatch.setattr(ds, "MAX_FILE_BYTES", 10)
    assert error_of(write(tmp_path / "c.csv", HEADER)).message == "Dataset file is larger than 5 MB"


def _deny(monkeypatch, method):
    real = getattr(Path, method)

    def denied(self, *args, **kwargs):
        if self.name == "locked.csv":
            raise PermissionError(13, "Permission denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, denied)


@pytest.mark.parametrize("method", ["read_bytes", "stat"])
def test_permission_error_is_a_dataset_error(tmp_path, monkeypatch, method):
    path = ds.write_dataset(tmp_path / "locked.csv", ds.generate_samples())
    _deny(monkeypatch, method)
    assert error_of(path).message == "Dataset file could not be read: locked.csv (Permission denied)"


def test_file_deleted_between_stat_and_read(tmp_path, monkeypatch):
    path = ds.write_dataset(tmp_path / "gone.csv", ds.generate_samples())

    def vanished(self):
        raise FileNotFoundError(2, "No such file")

    monkeypatch.setattr(Path, "read_bytes", vanished)
    assert error_of(path).message == "Dataset file not found: gone.csv"


# ------------------------------------------------------------------ overwrite guard
def test_missing_file_is_created(tmp_path):
    path = tmp_path / "sub" / "new.csv"
    assert ds.is_replaceable(path) == (True, "")
    ds.write_dataset(path, ds.generate_samples())
    assert ds.load_dataset(path).matches_generator
    assert not (tmp_path / "sub" / "new.csv.tmp").exists()


def test_malformed_synthetic_file_is_rewritten(tmp_path):
    path = write(tmp_path / "d.csv", "wrong,header\nSYN-1,SYN-T1,2,1\n\"SYN-2\",x\n")
    assert ds.is_replaceable(path) == (True, "")
    ds.write_dataset(path, ds.generate_samples())
    assert ds.load_dataset(path).samples == ds.generate_samples()


@pytest.mark.parametrize("content", [b"", HEADER.encode()])
def test_empty_or_header_only_file_is_replaceable(tmp_path, content):
    assert ds.is_replaceable(write(tmp_path / "d.csv", content)) == (True, "")


def test_non_synthetic_file_is_never_overwritten(tmp_path):
    path = write(tmp_path / "real.csv", REAL_ROWS)
    ok, reason = ds.is_replaceable(path)
    assert not ok and reason == "it contains rows that are not marked synthetic (SYN- prefix)"
    with pytest.raises(ds.DatasetError) as info:
        ds.write_dataset(path, ds.generate_samples())
    assert info.value.message.startswith("Refusing to overwrite real.csv: it contains rows that are not marked")
    assert path.read_bytes() == REAL_ROWS
    ds.write_dataset(path, ds.generate_samples(), overwrite_non_synthetic=True)
    assert ds.load_dataset(path).matches_generator


def test_unreadable_targets_are_refused(tmp_path, monkeypatch):
    folder = tmp_path / "folder.csv"
    folder.mkdir()
    assert ds.is_replaceable(folder) == (False, "it is not a regular file")
    bad = write(tmp_path / "bad.csv", HEADER.encode() + b"SYN-1,\xff\n")
    assert ds.is_replaceable(bad) == (False, "it is not UTF-8 text")
    locked = ds.write_dataset(tmp_path / "locked.csv", ds.generate_samples())
    _deny(monkeypatch, "read_bytes")
    assert ds.is_replaceable(locked) == (False, "it could not be read (Permission denied)")
    with pytest.raises(ds.DatasetError):
        ds.write_dataset(locked, ds.generate_samples())


def test_failed_write_keeps_old_file(tmp_path, monkeypatch):
    path = write(tmp_path / "d.csv", HEADER + "SYN-1,SYN-T1,1,1\n")
    before = path.read_bytes()

    def broken(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ds.os, "replace", broken)
    with pytest.raises(OSError):
        ds.write_dataset(path, ds.generate_samples())
    assert path.read_bytes() == before and not (tmp_path / "d.csv.tmp").exists()


# ------------------------------------------------------------------ paths
def test_dataset_path_is_isolated(tmp_path):
    path = ds.dataset_path()
    assert path.is_relative_to(tmp_path.resolve())
    assert not path.is_relative_to(PROJECT_ROOT / "data")
    assert ds.load_dataset().matches_generator  # conftest wrote the fixed-seed dataset there


def test_dataset_path_resolution(monkeypatch, tmp_path):
    monkeypatch.setenv("TPD_EVAL_DATASET", "data/other.csv")
    assert ds.dataset_path() == (PROJECT_ROOT / "data" / "other.csv").resolve()
    monkeypatch.setenv("TPD_EVAL_DATASET", "   ")
    assert ds.dataset_path() == (PROJECT_ROOT / "data" / "dummy_evaluation_dataset.csv").resolve()
    monkeypatch.delenv("TPD_EVAL_DATASET")
    import app.config

    env_file = write(tmp_path / ".env", f"TPD_EVAL_DATASET={tmp_path / 'from_env.csv'}\n")
    monkeypatch.setattr(app.config, "DOTENV_PATH", env_file)
    app.config.load_config()  # .env layering must not leak into this process-environment-only variable
    assert ds.dataset_path() == (PROJECT_ROOT / "data" / "dummy_evaluation_dataset.csv").resolve()


def test_display_path(tmp_path):
    assert ds.display_path(PROJECT_ROOT / "data" / "dummy_evaluation_dataset.csv") == \
        "data/dummy_evaluation_dataset.csv"
    assert ds.display_path(tmp_path / "x" / "mine.csv") == "mine.csv"


# ------------------------------------------------------------------ KPI separation
FORBIDDEN_IMPORTS = {"app.analytics", "app.trends", "dashboard.components.metrics"}


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            yield module
            for alias in node.names:
                yield f"{module}.{alias.name}" if module else alias.name


@pytest.mark.parametrize("rel", ["dashboard/components/metrics_panel.py", "app/evaluation_dataset.py"])
def test_metric_modules_do_not_import_kpi_sources(rel):
    names = set(_imports(ROOT / rel))
    assert not names & FORBIDDEN_IMPORTS, rel
    assert "metrics" not in names  # relative ``from . import metrics`` inside dashboard/components
    if rel.startswith("app/"):
        assert not any(n == "sqlite3" or n.startswith("app.database") for n in names), rel


# ------------------------------------------------------------------ CLI
def parse_counts(out):
    line = next(l for l in out.splitlines() if l.startswith("TP "))
    parts = line.replace("·", "").split()
    return dict(zip(parts[0::2], map(int, parts[1::2])))


def test_cli_writes_dataset_and_prints_evaluate_counts(tmp_path, capsys):
    out_path = tmp_path / "cli.csv"
    assert cli.main(["--output", str(out_path)]) == 0
    out = capsys.readouterr().out
    c = ds.evaluate(ds.load_dataset(out_path)).counts
    assert parse_counts(out) == {"TP": c.tp, "TN": c.tn, "FP": c.fp, "FN": c.fn}
    assert "Samples: 200 (seed 20260115)" in out
    assert "Synthetic evaluation data – not real passenger records." in out
    first = out_path.read_bytes()
    assert cli.main(["--output", str(out_path)]) == 0  # idempotent
    assert out_path.read_bytes() == first


def test_cli_defaults_to_dataset_path(capsys):
    assert cli.main([]) == 0
    assert str(ds.dataset_path()) in capsys.readouterr().out


def test_cli_seed_and_samples(tmp_path, capsys):
    out_path = tmp_path / "s.csv"
    assert cli.main(["--output", str(out_path), "--seed", "5", "--samples", "150"]) == 0
    assert ds.load_dataset(out_path).samples == ds.generate_samples(5, 150)
    assert "Samples: 150 (seed 5)" in capsys.readouterr().out


def test_cli_invalid_values_exit_1(tmp_path, capsys):
    assert cli.main(["--output", str(tmp_path / "x.csv"), "--samples", "5"]) == 1
    assert not (tmp_path / "x.csv").exists()
    with pytest.raises(SystemExit) as info:
        cli.main(["--samples", "abc"])
    assert info.value.code == 1


def test_cli_refuses_non_synthetic_without_force(tmp_path, capsys):
    path = write(tmp_path / "real.csv", REAL_ROWS)
    assert cli.main(["--output", str(path)]) == 2
    err = capsys.readouterr().err
    assert "Refusing to overwrite real.csv" in err and "Use --force to overwrite it anyway." in err
    assert path.read_bytes() == REAL_ROWS
    assert cli.main(["--output", str(path), "--force"]) == 0
    assert ds.load_dataset(path).matches_generator


def test_cli_os_error_exit_2(tmp_path, monkeypatch, capsys):
    def broken(src, dst):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(ds.os, "replace", broken)
    assert cli.main(["--output", str(tmp_path / "x.csv")]) == 2
    assert "Could not write" in capsys.readouterr().err


# ------------------------------------------------------------------ committed dataset (read-only)
def test_committed_dataset_matches_generator():
    assert COMMITTED.exists(), "run scripts/generate_evaluation_dataset.py --output data/dummy_evaluation_dataset.csv"
    loaded = ds.load_dataset(COMMITTED)
    assert loaded.samples == ds.generate_samples()
    assert loaded.matches_generator is True and loaded.is_synthetic
    assert len(loaded.samples) >= 100
    header = COMMITTED.read_text(encoding="utf-8").splitlines()[0]
    assert header == "sample_id,tracking_id,ground_truth,predicted_label"
