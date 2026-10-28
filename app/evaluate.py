"""Model evaluation CLI: measure person detection on a labelled test set and store the result.

    python -m app.evaluate images --images DIR [--labels DIR] [--model PATH] [--conf X] [--max-images N]
    python -m app.evaluate mot --gt gt.txt --pred pred.txt (--model PATH | --model-version TEXT) [--num-frames N]
    python -m app.evaluate import --json results.json
    python -m app.evaluate list [--limit N]

(or scripts\\run_evaluation.bat ...). Results go to the ``model_evaluations``
table of the same database as the dashboard (``--db`` > ``TPD_DB_PATH`` >
``database.path``). Positive class: person; box matching at IoU >= --iou
(default 0.5); see docs/limitations.md "Evaluation method".

Exit codes: 0 ok, 1 config/dataset/usage, 2 argument error (argparse),
3 database, 4 model, 5 unexpected. Nothing is stored unless the whole run succeeds.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.metadata
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from app.config import PROJECT_ROOT, configure_logging, load_config, resolve_db_path, resolve_path
from app.database import Database, DatabaseError, transaction, utc_now
from app.detector import ModelNotFoundError, load_model, parse_person_boxes, resolve_device
from app.evaluation import (DEFAULT_IOU, MAX_NOTES, MAX_TEXT, Counts, EvaluationInputError, EvaluationRecord,
                            StoredEvaluation, imported_evaluated_at, list_evaluations, model_identity, parse_mot,
                            parse_yolo_labels, record_from_import, scores, store_evaluation)
from app.settings_schema import ConfigError, DetectionConfig

logger = logging.getLogger("app.evaluate")

EXIT_OK, EXIT_INPUT, EXIT_DATABASE, EXIT_MODEL, EXIT_UNEXPECTED = 0, 1, 3, 4, 5
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp")
MAX_JSON_BYTES = 1024 * 1024
IGNORED_GT = "dropped; overlapping predictions count as FP"


# ------------------------------------------------------------------ argument types
def _int_min(minimum: int, maximum: Optional[int] = None) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from None
        if number < minimum or (maximum is not None and number > maximum):
            raise argparse.ArgumentTypeError(f"must be {minimum}..{maximum}" if maximum else f"must be >= {minimum}")
        return number
    return parse


def _iou(value: str) -> float:
    number = float(value)
    if not 0 < number <= 1:
        raise argparse.ArgumentTypeError("IoU threshold must be 0 < x <= 1")
    return number


def _confidence(value: str) -> float:  # same range as app.main --conf
    number = float(value)
    if not 0.05 <= number <= 0.95:
        raise argparse.ArgumentTypeError("confidence must be 0.05..0.95")
    return number


def _imgsz(value: str) -> int:  # same rule as detection.imgsz
    number = _int_min(160, 1920)(value)
    if number % 32:
        raise argparse.ArgumentTypeError("imgsz must be a multiple of 32")
    return number


def _classes(value: str) -> tuple:
    try:
        items = tuple(int(p) for p in value.split(",") if p.strip())
    except ValueError:
        raise argparse.ArgumentTypeError("expected comma-separated class ids, e.g. 1,2,7") from None
    if not items or any(i < 0 for i in items):
        raise argparse.ArgumentTypeError("class ids must be integers >= 0")
    return items


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None, help="config YAML (default config/config.yaml or $TPD_CONFIG)")
    common.add_argument("--db", default=None, help="SQLite database (default TPD_DB_PATH or database.path)")
    common.add_argument("--dataset-name", default=None, help="name stored with the result (default: folder/file)")
    common.add_argument("--notes", default=None, help=f"free text, at most {MAX_NOTES} characters")
    common.add_argument("--iou", type=_iou, default=DEFAULT_IOU, help="box match IoU threshold (default 0.5)")
    common.add_argument("--dry-run", action="store_true", help="compute and print only; store nothing")
    common.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    p = argparse.ArgumentParser(prog="python -m app.evaluate",
                                description="Evaluate person detection on labelled data (positive class: person).")
    sub = p.add_subparsers(dest="command", required=True)
    images = sub.add_parser("images", parents=[common], help="run the configured YOLO model on YOLO-labelled images")
    images.add_argument("--images", required=True, help="folder of .jpg/.jpeg/.png/.bmp images")
    images.add_argument("--labels", default=None, help="YOLO label folder (default: sibling 'labels' folder)")
    images.add_argument("--person-class", type=_int_min(0), default=0, help="label class id of person (default 0)")
    images.add_argument("--model", default=None, help="YOLO weights (default detection.model_path)")
    images.add_argument("--conf", type=_confidence, default=None, help="confidence (default detection.confidence)")
    images.add_argument("--device", default=None, help="cpu | auto | cuda:N")
    images.add_argument("--imgsz", type=_imgsz, default=None, help="inference size, 160..1920, multiple of 32")
    images.add_argument("--max-images", type=_int_min(1), default=None, help="only the first N images by name")

    mot = sub.add_parser("mot", parents=[common], help="compare a MOT ground-truth file with --export-mot output",
                         description="Ground-truth rows with conf 0 or a class outside --gt-classes are dropped "
                                     "before matching; predictions overlapping them count as FP (strict rule). "
                                     "CVAT 'MOT 1.1' exports use their own label index in the class column: pass "
                                     "--gt-classes with the person label's index.")
    mot.add_argument("--gt", required=True, help="ground truth frame,id,x,y,w,h[,conf,class,vis]")
    mot.add_argument("--pred", required=True, help="predictions written by app.main --export-mot")
    who = mot.add_mutually_exclusive_group(required=True)
    who.add_argument("--model", default=None, help="the weights file that produced --pred (hashed)")
    who.add_argument("--model-version", default=None, help="model version text when the file is not available")
    mot.add_argument("--model-name", default="unknown", help="model name stored with --model-version")
    mot.add_argument("--conf", type=_confidence, default=None, help="confidence the producing run used")
    mot.add_argument("--num-frames", type=_int_min(1), default=None,
                     help="total frames; frames absent from both files count as image-level TN")
    mot.add_argument("--gt-classes", type=_classes, default=(1, 2, 7), help="person classes in GT (default 1,2,7)")

    imp = sub.add_parser("import", parents=[common], help="store counts from a JSON file (metrics recomputed)")
    imp.add_argument("--json", required=True, help="UTF-8 JSON object, at most 1 MB")

    lst = sub.add_parser("list", parents=[common], help="print stored evaluations, newest first")
    lst.add_argument("--limit", type=_int_min(1, 1000), default=10)
    return p


# ------------------------------------------------------------------ helpers
def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _check_text(value: Optional[str], name: str, limit: int) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    if not value or len(value) > limit:
        raise EvaluationInputError(f"{name}: expected 1..{limit} characters")
    return value


def _pct(value: Optional[float], reason: str) -> str:
    return f"{value * 100:.1f}%" if value is not None else f"N/A ({reason})"


def _summary(record: EvaluationRecord) -> str:
    c, s = record.counts, scores(record.counts)
    conf = "unknown" if record.conf_threshold is None else f"{record.conf_threshold:g}"
    lines = [f"Dataset: {record.dataset_name}",
             f"Model: {record.model_name} ({record.model_version}) | conf {conf}",
             f"Test set: {c.images} images/frames | IoU >= {record.iou_threshold:g} | micro average",
             f"Boxes: TP {c.tp} FP {c.fp} FN {c.fn} (GT {c.gt_boxes}, predicted {c.pred_boxes})"]
    if c.img_tp is None:
        lines.append("Image level: TN unknown (pass --num-frames for MOT)")
    else:
        lines.append(f"Image level: TP {c.img_tp} FP {c.img_fp} FN {c.img_fn} TN {c.img_tn}")
    lines += [f"Accuracy Score (image-level presence): {_pct(s.accuracy, 'TN unknown or no images')}",
              f"Precision: {_pct(s.precision, 'no predictions')}",
              f"Recall (Sensitivity): {_pct(s.recall, 'no ground-truth persons')}",
              f"F1-Score: {_pct(s.f1, 'precision or recall is N/A')}"]
    return "\n".join(lines)


def _images_record(args: argparse.Namespace, cfg) -> EvaluationRecord:
    import cv2  # only needed for this subcommand

    images_dir = resolve_path(args.images)
    if not images_dir.is_dir():
        raise EvaluationInputError(f"--images: folder not found: {args.images}")
    if args.labels:
        labels_dir = resolve_path(args.labels)
    else:
        parts = list(images_dir.parts)
        idx = max((i for i, p in enumerate(parts) if p.lower() == "images"), default=None)
        if idx is None:
            raise EvaluationInputError("--labels is required when the images folder is not named 'images'")
        parts[idx] = "labels"
        labels_dir = Path(*parts)
    if not labels_dir.is_dir():
        raise EvaluationInputError(f"--labels: folder not found: {labels_dir}")
    if not any(labels_dir.glob("*.txt")):
        raise EvaluationInputError(f"no label files found in {labels_dir}")
    files = sorted(p for p in images_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS)
    if args.max_images:
        files = files[:args.max_images]
    if not files:
        raise EvaluationInputError(f"no images found in {images_dir}")

    detection = dataclasses.asdict(cfg.detection)
    for key, value in (("model_path", args.model), ("confidence", args.conf), ("device", args.device)):
        if value is not None:
            detection[key] = value
    det = DetectionConfig.from_dict(detection)
    imgsz = args.imgsz or det.imgsz
    model = load_model(det)
    device = resolve_device(det.device)
    counts = Counts()
    for n, path in enumerate(files, start=1):
        frame = cv2.imread(str(path))
        if frame is None:
            raise EvaluationInputError(f"cannot read image {path}")
        height, width = frame.shape[:2]
        gt = parse_yolo_labels(labels_dir / f"{path.stem}.txt", width, height, args.person_class)
        results = model.predict(frame, classes=[0], conf=det.confidence, iou=det.iou, imgsz=imgsz, device=device,
                                verbose=False)
        pred = [(b.bbox, b.confidence) for b in parse_person_boxes(results, require_ids=False)]
        counts = counts.add_frame(gt, pred, args.iou)
        if n % 50 == 0:
            print(f"  {n}/{len(files)} images", flush=True)
    name, version = model_identity(resolve_path(det.model_path))
    try:
        ultralytics_version = importlib.metadata.version("ultralytics")
    except importlib.metadata.PackageNotFoundError:
        ultralytics_version = "not installed"
    config = {"images_dir": _display_path(images_dir), "labels_dir": _display_path(labels_dir),
              "person_class": args.person_class, "conf": det.confidence, "nms_iou": det.iou, "imgsz": imgsz,
              "device": device, "ultralytics": ultralytics_version, "matching": "greedy-confidence one-to-one",
              "iou_threshold": args.iou, "stage": "detection", "ignored_gt": IGNORED_GT}
    return EvaluationRecord("images", name, version, args.dataset_name or images_dir.parent.name or images_dir.name,
                            counts, args.iou, det.confidence, config, args.notes)


def _mot_record(args: argparse.Namespace) -> EvaluationRecord:
    gt_path, pred_path = resolve_path(args.gt), resolve_path(args.pred)
    for flag, path in (("--gt", gt_path), ("--pred", pred_path)):
        if not path.is_file():
            raise EvaluationInputError(f"{flag}: file not found: {path}")
    if args.model:
        model_path = resolve_path(args.model)
        if not model_path.is_file():
            raise EvaluationInputError(f"--model: file not found: {model_path}")
        name, version = model_identity(model_path)
    else:
        name = _check_text(args.model_name, "--model-name", MAX_TEXT)
        version = _check_text(args.model_version, "--model-version", MAX_TEXT)
    gt = parse_mot(gt_path, ground_truth=True, gt_classes=args.gt_classes)
    pred = parse_mot(pred_path, ground_truth=False)
    if args.num_frames:
        beyond = [f for f in (*gt, *pred) if f > args.num_frames]
        if beyond:
            raise EvaluationInputError(f"frame {max(beyond)} is beyond --num-frames {args.num_frames}")
        frames = range(1, args.num_frames + 1)
    else:
        frames = sorted(set(gt) | set(pred))
        if not frames:
            raise EvaluationInputError("no frames in --gt or --pred")
    counts = Counts() if args.num_frames else Counts(img_tp=None, img_fp=None, img_fn=None, img_tn=None)
    for f in frames:
        counts = counts.add_frame([b for b, _ in gt.get(f, [])], pred.get(f, []), args.iou,
                                  presence_known=bool(args.num_frames))
    config = {"stage": "tracked (MOT export)", "gt_file": gt_path.name, "pred_file": pred_path.name,
              "gt_classes": list(args.gt_classes), "num_frames": args.num_frames, "iou_threshold": args.iou,
              "matching": "greedy-confidence one-to-one", "ignored_gt": IGNORED_GT}
    return EvaluationRecord("mot", name, version, args.dataset_name or gt_path.stem, counts, args.iou, args.conf,
                            config, args.notes)


def _import_record(args: argparse.Namespace):
    path = resolve_path(args.json)
    if not path.is_file():
        raise EvaluationInputError(f"--json: file not found: {path}")
    if path.stat().st_size > MAX_JSON_BYTES:
        raise EvaluationInputError("--json: file is larger than 1 MB")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EvaluationInputError(f"--json: not valid UTF-8 JSON: {exc}") from None
    record = record_from_import(data, path.name)
    if args.dataset_name or args.notes:
        record = dataclasses.replace(record, dataset_name=args.dataset_name or record.dataset_name,
                                     notes=args.notes or record.notes)
    return record, imported_evaluated_at(data)


def _print_list(rows: Sequence[StoredEvaluation]) -> None:
    if not rows:
        print("No stored evaluations. Run: scripts\\run_evaluation.bat images --images <folder>")
        return
    for ev in rows:
        s = ev.scores
        print(f"#{ev.evaluation_id} {ev.evaluated_at:%Y-%m-%d %H:%M} UTC | {ev.method} | {ev.model_name} "
              f"({ev.model_version}) | {ev.dataset_name} | n={ev.test_set_size} | "
              f"Acc {_pct(s.accuracy, 'n/a')} P {_pct(s.precision, 'n/a')} R {_pct(s.recall, 'n/a')} "
              f"F1 {_pct(s.f1, 'n/a')}")


# ------------------------------------------------------------------ main
def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        configure_logging(cfg.logging, "evaluate.log")
        if args.log_level:
            logging.getLogger().setLevel(args.log_level)
        db_path = resolve_db_path(args.db, cfg)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_INPUT
    try:
        evaluated_at = None
        if args.command != "list":
            _check_text(args.notes, "--notes", MAX_NOTES)
            _check_text(args.dataset_name, "--dataset-name", MAX_TEXT)
        if args.command == "images":
            record = _images_record(args, cfg)
        elif args.command == "mot":
            record = _mot_record(args)
        elif args.command == "import":
            record, evaluated_at = _import_record(args)
        else:
            record = None
        if record is not None:
            print(_summary(record))
            if args.dry_run:
                print("Dry run: nothing stored")
                return EXIT_OK
        database = Database(db_path, cfg.database.busy_timeout_ms)
        database.initialize()
        conn = database.connect()
        try:
            if record is None:
                _print_list(list_evaluations(conn, args.limit))
                return EXIT_OK
            with transaction(conn):
                new_id = store_evaluation(conn, record, evaluated_at or utc_now())
        finally:
            conn.close()
        print(f"Stored evaluation #{new_id} in {db_path}")
        return EXIT_OK
    except KeyboardInterrupt:
        print("Interrupted – nothing stored.", file=sys.stderr)
        return EXIT_INPUT
    except (EvaluationInputError, ConfigError) as exc:
        logger.error("%s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_INPUT
    except ModelNotFoundError as exc:
        logger.error("%s", exc)
        print(str(exc), file=sys.stderr)
        return EXIT_MODEL
    except (DatabaseError, sqlite3.Error, OSError) as exc:
        logger.error("Database error (%s): %s", db_path, exc)
        print(f"Database error ({db_path}): {exc}", file=sys.stderr)
        return EXIT_DATABASE
    except Exception:
        logger.critical("Unexpected error", exc_info=True)
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
