# embedding 客户端；无 EMBEDDING_API_KEY 时向量通道整体跳过
"""Provider-agnostic embedding client.

Configured by EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL. Legacy
OPENROUTER_* / WIKI_EMBEDDING_API_KEY key names still work but warn once, so an
older .env keeps running while it is migrated.
"""

from __future__ import annotations

import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "qwen/qwen3-embedding-8b"
DEFAULT_PROVIDER = "openrouter"

_TIMEOUT_SECONDS = 180.0
_MAX_ATTEMPTS = 3
_PRIMARY_KEY_VARIABLE = "EMBEDDING_API_KEY"
_LEGACY_KEY_VARIABLES = ("OPENROUTER_KEY", "OPENROUTER_API_KEY", "WIKI_EMBEDDING_API_KEY")
_KEY_VARIABLES = (_PRIMARY_KEY_VARIABLE, *_LEGACY_KEY_VARIABLES)

logger = logging.getLogger(__name__)

_warned_legacy: set[str] = set()


def load_dotenv(root: Path) -> None:
    root = Path(root).resolve()
    # Isolated registered KBs share application credentials. Do not traverse
    # arbitrary ancestors for unrelated standalone knowledge bases.
    from wiki_maintainer.project import knowledge_base_entries
    app_root = None
    for parent in root.parents:
        if (parent / "mobilework.config.json").is_file():
            if any(entry["root"] == root for entry in knowledge_base_entries(parent).values()):
                app_root = parent
            break
    if app_root is not None:
        load_dotenv(app_root)
    path = root / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def _setting(name: str, default: str) -> str:
    # Empty values in .env mean "not configured", not "use an empty string".
    return (os.environ.get(name) or "").strip() or default


def base_url() -> str:
    return _setting("EMBEDDING_BASE_URL", DEFAULT_BASE_URL)


def provider() -> str:
    value = _setting("EMBEDDING_PROVIDER", DEFAULT_PROVIDER).lower()
    if value not in {"openrouter", "local"}:
        raise RuntimeError(f"unsupported EMBEDDING_PROVIDER: {value}")
    return value


def model_name() -> str:
    if provider() == "local":
        from . import local_embedding

        return local_embedding.runtime_name()
    return _setting("EMBEDDING_MODEL", DEFAULT_MODEL)


def preprocess_id() -> str:
    if provider() == "local":
        from . import local_embedding

        return local_embedding.preprocess_id()
    return "remote-raw-v1"


def endpoint() -> str:
    base = base_url()
    return urljoin(base if base.endswith("/") else base + "/", "embeddings")


def _api_key() -> str:
    for name in _KEY_VARIABLES:
        value = (os.environ.get(name) or "").strip()
        if not value:
            continue
        if name != _PRIMARY_KEY_VARIABLE and name not in _warned_legacy:
            _warned_legacy.add(name)
            logger.warning(
                "using legacy embedding key variable %s; rename it to %s",
                name,
                _PRIMARY_KEY_VARIABLE,
            )
        return value
    raise RuntimeError(f"{_PRIMARY_KEY_VARIABLE} is not set")


def has_api_key() -> bool:
    """Lets the vector channel report `disabled` instead of attempting a call."""
    if provider() == "local":
        from . import local_embedding

        return local_embedding.available()
    return any((os.environ.get(name) or "").strip() for name in _KEY_VARIABLES)


def available() -> bool:
    """Whether the configured backend can run without making a probe request."""
    return has_api_key()


def _optional_headers() -> dict[str, str]:
    headers = {}
    referer = os.environ.get("OPENROUTER_HTTP_REFERER")
    if referer is not None:
        headers["HTTP-Referer"] = referer
    title = os.environ.get("OPENROUTER_APP_TITLE")
    if title is not None:
        headers["X-OpenRouter-Title"] = title
    return headers


def _parse_vectors(body: Any, expected: int) -> list[list[float]]:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("missing embedding data")
    indexed: list[tuple[int, list[float]]] = []
    for fallback, item in enumerate(data):
        raw_index = item.get("index") if isinstance(item, dict) else None
        index = raw_index if isinstance(raw_index, int) else fallback
        vector_data = item.get("embedding") if isinstance(item, dict) else None
        if not isinstance(vector_data, list):
            raise RuntimeError(f"missing embedding at index {index}")
        try:
            vector = [float(value) for value in vector_data]
        except (TypeError, ValueError) as error:
            raise RuntimeError("non-number embedding value") from error
        if not vector or any(not math.isfinite(value) for value in vector):
            raise RuntimeError(f"invalid embedding at index {index}")
        indexed.append((index, vector))
    indexed.sort(key=lambda pair: pair[0])
    if len(indexed) != expected:
        raise RuntimeError(
            f"embedding count mismatch: expected {expected}, got {len(indexed)}"
        )
    return [vector for _, vector in indexed]


def fetch_batch(texts: list[str], *, query: bool = False) -> tuple[list[list[float]], float]:
    import httpx

    if not texts:
        return [], 0.0
    if provider() == "local":
        from . import local_embedding

        start = time.perf_counter()
        vectors = local_embedding.encode(texts, query=query)
        return vectors, (time.perf_counter() - start) * 1000.0
    url = endpoint()
    payload = {"model": model_name(), "input": list(texts), "encoding_format": "float"}
    key = _api_key()
    headers = _optional_headers()

    start = time.perf_counter()
    last: Optional[BaseException] = None
    with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = client.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {key}", **headers},
                )
            except Exception as error:  # transport failure is retryable
                last = error
                logger.warning("embedding request failed: %s", redact(str(error)))
            else:
                try:
                    body = response.json()
                except ValueError as error:
                    raise RuntimeError("invalid embedding response") from error
                if response.is_success:
                    vectors = _parse_vectors(body, len(texts))
                    return vectors, (time.perf_counter() - start) * 1000.0
                last = RuntimeError(
                    redact(f"embedding HTTP {response.status_code}: {body}")
                )
                logger.warning("embedding rejected: %s", last)
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(float(1 << attempt))

    if last is not None:
        raise last
    raise RuntimeError("embedding request failed")


def fetch(text: str) -> tuple[list[float], float]:
    vectors, elapsed_ms = fetch_batch([text], query=True)
    return vectors[0], elapsed_ms


def redact(msg: str) -> str:
    redacted = msg
    for name in _KEY_VARIABLES:
        secret = (os.environ.get(name) or "").strip()
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted[:500]
