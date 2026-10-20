"""Configuration loading: built-in defaults < config.yaml < .env < process env (TPD_*).

CLI flags are applied on top by the entry points (``app.main`` and scripts).
Every relative path is resolved against :data:`PROJECT_ROOT`, never the CWD.

    from app.config import load_config, resolve_db_path
    cfg = load_config()                 # config/config.yaml (or $TPD_CONFIG)
    db_path = resolve_db_path(args.db, cfg)
"""

from __future__ import annotations

import copy
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Union

import yaml

from app.settings_schema import AppConfig, ConfigError, LoggingConfig

__all__ = [
    "PROJECT_ROOT", "DEFAULT_CONFIG_PATH", "DOTENV_PATH", "DEFAULT_DB_PATH", "AppConfig", "ConfigError",
    "load_config", "resolve_path", "resolve_db_path", "configure_logging",
]

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
DOTENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_DB_PATH = "data/ticketless.db"
DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")
ENV_PREFIX = "TPD_"
MEMORY_DB = ":memory:"

# Environment variable -> dotted location in the YAML structure.
ENV_OVERRIDES: Dict[str, Sequence[str]] = {
    "TPD_SOURCE": ("camera", "source_type"),
    "TPD_PHONE_URL": ("camera", "phone", "url"),
    "TPD_CAMERA_INDEX": ("camera", "webcam", "index"),
    "TPD_VIDEO_PATH": ("camera", "video", "path"),
    "TPD_MODEL_PATH": ("detection", "model_path"),
    "TPD_DEVICE": ("detection", "device"),
    "TPD_DB_PATH": ("database", "path"),
    "TPD_LOG_LEVEL": ("logging", "level"),
    "TPD_CURRENT_STATION": ("journey", "current_station"),
}

_CONSOLE_HANDLER = "tpd-console"
_FILE_HANDLER_PREFIX = "tpd-file:"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


# ------------------------------------------------------------------- paths
def resolve_path(value: Union[str, Path]) -> Path:
    """Expand ``~`` and resolve a relative path against the project root."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def resolve_db_path(cli_value: Optional[str] = None, cfg: Optional[AppConfig] = None) -> Union[Path, str]:
    """The single DB-path rule: ``--db`` > ``$TPD_DB_PATH`` > ``database.path`` > default.

    ``":memory:"`` is returned unchanged; anything else becomes an absolute path
    that must end in .db, .sqlite or .sqlite3 and must not be a directory.
    """
    candidates = (cli_value, os.environ.get("TPD_DB_PATH"), cfg.database.path if cfg else None, DEFAULT_DB_PATH)
    value = next(str(c).strip() for c in candidates if c is not None and str(c).strip())
    if value == MEMORY_DB:
        return value
    path = resolve_path(value)
    if path.is_dir():
        raise ConfigError(f"database.path: {value!r} is a directory; give a file such as data/ticketless.db")
    if path.suffix.lower() not in DB_SUFFIXES:
        raise ConfigError(f"database.path: {value!r} must end in {', '.join(DB_SUFFIXES)}")
    return path


# ----------------------------------------------------------------- loading
def _read_dotenv(path: Optional[Path]) -> Dict[str, str]:
    """Minimal ``.env`` parser: ``KEY=VALUE`` lines, ``#`` comments, optional quotes."""
    if path is None or not Path(path).is_file():
        return {}
    values: Dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        logger.warning("Could not read %s (%s); ignoring it.", path, exc)
        return {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        logger.warning("Config file %s not found; using built-in defaults.", path)
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config: invalid YAML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"config: cannot read {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"config: top level of {path} must be a mapping, got {type(data).__name__}")
    return data


def _set_nested(raw: Dict[str, Any], keys: Sequence[str], value: Any) -> None:
    node = raw
    for i, key in enumerate(keys[:-1]):
        child = node.get(key)
        if child is None:
            child = node[key] = {}
        elif not isinstance(child, dict):
            raise ConfigError(f"{'.'.join(keys[: i + 1])}: expected a mapping, got {type(child).__name__}")
        node = child
    node[keys[-1]] = value


def _env_value(name: str, value: str) -> Any:
    if name == "TPD_CAMERA_INDEX":
        try:
            return int(value)
        except ValueError:
            raise ConfigError(f"TPD_CAMERA_INDEX: expected an integer webcam index, got {value!r}") from None
    if name == "TPD_CURRENT_STATION" and value.lower() in ("none", "null"):
        return None
    return value


def _apply_env_overrides(raw: Dict[str, Any], env: Mapping[str, str]) -> None:
    for name, keys in ENV_OVERRIDES.items():
        value = str(env.get(name) or "").strip()
        if value:
            _set_nested(raw, keys, _env_value(name, value))


def load_config(path: Union[str, Path, None] = None, *, environ: Optional[Mapping[str, str]] = None,
                dotenv_path: Union[str, Path, None] = None) -> AppConfig:
    """Load and validate the full application config.

    ``path`` > ``$TPD_CONFIG`` > ``config/config.yaml``. A missing YAML logs a
    WARNING and uses defaults; invalid YAML or values raise :class:`ConfigError`.
    Process environment variables win over the ``.env`` file.
    """
    env_source = os.environ if environ is None else environ
    dotenv = _read_dotenv(Path(dotenv_path) if dotenv_path is not None else DOTENV_PATH)
    layered = {k: v for k, v in dotenv.items() if k.startswith(ENV_PREFIX)}
    layered.update({k: v for k, v in env_source.items() if k.startswith(ENV_PREFIX)})

    if path is None:
        path = layered.get("TPD_CONFIG") or DEFAULT_CONFIG_PATH
    config_path = resolve_path(path)
    raw = copy.deepcopy(_read_yaml(config_path))
    _apply_env_overrides(raw, layered)
    return AppConfig.from_dict(raw, config_path=config_path if config_path.is_file() else None)


# ----------------------------------------------------------------- logging
def configure_logging(logging_cfg: LoggingConfig, filename: str = "app.log") -> None:
    """Console + rotating file (1 MB x 3) handlers on the root logger.

    Idempotent: calling it again only updates the level. If the log directory
    is not writable a WARNING is logged and logging stays console-only.
    """
    root = logging.getLogger()
    level = getattr(logging, logging_cfg.level)
    root.setLevel(level)
    names = {h.get_name() for h in root.handlers}
    formatter = logging.Formatter(_LOG_FORMAT)

    if _CONSOLE_HANDLER not in names:
        console = logging.StreamHandler()
        console.set_name(_CONSOLE_HANDLER)
        console.setFormatter(formatter)
        root.addHandler(console)

    file_name = _FILE_HANDLER_PREFIX + filename
    if file_name not in names:
        log_dir = resolve_path(logging_cfg.dir)
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(log_dir / filename, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        except OSError as exc:
            logger.warning("Log directory %s is not writable (%s); logging to the console only.", log_dir, exc)
        else:
            handler.set_name(file_name)
            handler.setFormatter(formatter)
            root.addHandler(handler)

    for handler in root.handlers:
        if (handler.get_name() or "").startswith("tpd-"):
            handler.setLevel(level)
