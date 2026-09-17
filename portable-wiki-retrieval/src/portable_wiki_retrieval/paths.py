from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "portable-wiki-retrieval"


def distribution_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "config.example.toml").is_file() and (parent / "skills").is_dir():
            return parent
    return current.parent


def default_config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME


def default_state_dir() -> Path:
    override = os.environ.get("PORTABLE_WIKI_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / APP_NAME / "state"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME / "state"
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / APP_NAME


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.environ.get("PORTABLE_WIKI_CONFIG"):
        return Path(os.environ["PORTABLE_WIKI_CONFIG"]).expanduser().resolve()
    portable = distribution_root() / "config.toml"
    if portable.is_file():
        return portable
    return default_config_dir() / "config.toml"


def load_dotenv(config_path: Path | None = None, *, override: bool = False) -> list[Path]:
    """Load known local .env files; optionally make the selected config's .env authoritative."""
    candidates = [distribution_root() / ".env"]
    if config_path is not None:
        candidates.append(config_path.resolve().parent / ".env")
    loaded: list[Path] = []
    for path in dict.fromkeys(candidates):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if not value or value.startswith("#") or "=" not in value:
                continue
            key, raw = value.split("=", 1)
            key = key.strip()
            if key and (override or key not in os.environ):
                os.environ[key] = raw.strip().strip('"').strip("'")
        loaded.append(path)
    return loaded
