"""Small in-process ONNX embedding backend for Windows and CPU-only hosts."""

from __future__ import annotations

import os
import threading
from functools import lru_cache
from pathlib import Path

import numpy as np

MODEL_ID = "BAAI/bge-small-zh-v1.5"
MODEL_DIM = 512
MAX_LENGTH = 512
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
DEFAULT_VARIANT = "onnx/model_quantized.onnx"

_inference_lock = threading.Lock()


def default_model_dir() -> Path:
    root = Path(os.environ.get("MOBILEWORK_ROOT") or Path(__file__).resolve().parents[1])
    return root.resolve() / ".mobilework-models" / "bge-small-zh-v1.5"


def model_dir() -> Path:
    configured = (os.environ.get("EMBEDDING_LOCAL_MODEL_PATH") or "").strip()
    if not configured:
        return default_model_dir()
    path = Path(configured).expanduser()
    if not path.is_absolute():
        project = Path(os.environ.get("MOBILEWORK_ROOT") or Path(__file__).resolve().parents[1])
        path = project / path
    return path.resolve()


def variant() -> str:
    return (os.environ.get("EMBEDDING_LOCAL_VARIANT") or "").strip() or DEFAULT_VARIANT


def model_file() -> Path:
    return model_dir() / variant()


def available() -> bool:
    root = model_dir()
    return model_file().is_file() and (root / "tokenizer.json").is_file()


def runtime_name() -> str:
    name = Path(variant()).stem.replace("model_", "")
    if name == "model":
        name = "fp32"
    return f"local/{MODEL_ID}-{name}"


def preprocess_id() -> str:
    return f"bge-cls-l2:qprefix-v1:max{MAX_LENGTH}"


@lru_cache(maxsize=4)
def _runtime(root_value: str, variant_value: str, threads: int):
    import onnxruntime as ort
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    from transformers import AutoTokenizer

    root = Path(root_value)
    file = root / variant_value
    if not file.is_file():
        raise RuntimeError(
            f"local embedding model is missing: {file}; run the local model setup first"
        )
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = ort.InferenceSession(
        str(file), sess_options=options, providers=["CPUExecutionProvider"]
    )
    tokenizer = AutoTokenizer.from_pretrained(root, local_files_only=True)
    return tokenizer, session


def clear_runtime_cache() -> None:
    _runtime.cache_clear()


def _thread_count() -> int:
    configured = (os.environ.get("EMBEDDING_LOCAL_THREADS") or "").strip()
    if configured:
        return max(1, int(configured))
    return min(8, max(1, os.cpu_count() or 1))


def encode(texts: list[str], *, query: bool = False) -> list[list[float]]:
    if not texts:
        return []
    root = model_dir()
    tokenizer, session = _runtime(str(root), variant(), _thread_count())
    prepared = [QUERY_PREFIX + text if query else text for text in texts]
    tokens = tokenizer(
        prepared,
        padding=True,
        truncation=True,
        max_length=MAX_LENGTH,
        return_tensors="np",
    )
    feeds = {
        item.name: tokens[item.name].astype(np.int64, copy=False)
        for item in session.get_inputs()
    }
    with _inference_lock:
        hidden = session.run(None, feeds)[0]
    vectors = hidden[:, 0, :].astype(np.float32, copy=False)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors = vectors / np.maximum(norms, 1e-12)
    if not np.isfinite(vectors).all() or vectors.shape[1] != MODEL_DIM:
        raise RuntimeError(f"invalid local embedding output shape: {vectors.shape}")
    return vectors.tolist()
