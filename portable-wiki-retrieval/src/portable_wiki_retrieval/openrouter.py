from __future__ import annotations

import math
import os
import time
from typing import Any

import httpx

from .errors import RetrievalError


class OpenRouterClient:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    @staticmethod
    def _key(section: dict[str, Any], error_code: str) -> str:
        env_name = section.get("api_key_env", "OPENROUTER_API_KEY")
        value = os.environ.get(env_name, "").strip()
        if not value:
            raise RetrievalError(error_code, "未配置所需的外部模型凭据")
        return value

    @staticmethod
    def _redact(message: str, key: str) -> str:
        return message.replace(key, "[redacted]")[:500]

    def embedding_available(self) -> bool:
        section = self.config["embedding"]["remote"]
        return bool(os.environ.get(section["api_key_env"], "").strip())

    def reranker_available(self) -> bool:
        section = self.config["reranker"]["remote"]
        return bool(os.environ.get(section["api_key_env"], "").strip())

    @staticmethod
    def _post(url: str, *, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = httpx.post(url, headers=headers, json=payload, timeout=timeout)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        time.sleep(0.5 * (2 ** attempt))
                        continue
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.TransportError) as error:
                last_error = error
                if attempt < 2:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise
        assert last_error is not None
        raise last_error

    def embed(self, texts: list[str], *, query: bool = False) -> tuple[list[list[float]], float]:
        if not texts:
            return [], 0.0
        section = self.config["embedding"]["remote"]
        key = self._key(section, "VECTOR_UNAVAILABLE")
        payload = {
            "model": section["model"], "input": texts, "encoding_format": "float",
            "input_type": "search_query" if query else "search_document",
        }
        started = time.perf_counter()
        try:
            response = self._post(
                f"{section['base_url'].rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                payload=payload, timeout=section["timeout_ms"] / 1000,
            )
            response.raise_for_status()
            body = response.json()
            rows = sorted(body["data"], key=lambda item: item.get("index", 0))
            vectors = [[float(value) for value in row["embedding"]] for row in rows]
            if len(vectors) != len(texts) or not vectors or any(
                not vector or any(not math.isfinite(value) for value in vector) for vector in vectors
            ):
                raise ValueError("embedding 响应数量或数值无效")
            dimension = len(vectors[0])
            if any(len(vector) != dimension for vector in vectors):
                raise ValueError("embedding 维度不一致")
            return vectors, (time.perf_counter() - started) * 1000
        except RetrievalError:
            raise
        except Exception as error:
            raise RetrievalError("VECTOR_UNAVAILABLE", self._redact(f"OpenRouter embedding 失败：{error}", key)) from error

    def rerank(self, query: str, documents: list[str], top_n: int) -> tuple[list[dict[str, Any]], float]:
        section = self.config["reranker"]["remote"]
        key = self._key(section, "RERANKER_UNAVAILABLE")
        started = time.perf_counter()
        try:
            response = self._post(
                f"{section['base_url'].rstrip('/')}/rerank",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                payload={"model": section["model"], "query": query, "documents": documents, "top_n": top_n},
                timeout=self.config["reranker"]["timeout_ms"] / 1000,
            )
            response.raise_for_status()
            rows = response.json()["results"]
            result = [{"index": int(row["index"]), "score": float(row["relevance_score"])} for row in rows]
            return result, (time.perf_counter() - started) * 1000
        except RetrievalError:
            raise
        except Exception as error:
            raise RetrievalError("RERANKER_UNAVAILABLE", self._redact(f"OpenRouter reranker 失败：{error}", key)) from error
