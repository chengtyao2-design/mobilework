# 同 (scope, channels, normalized_query) 重复调用检测，返回 duplicate 标记，防 High 档空转
"""In-process duplicate-call detection.

An agent on a high reasoning budget tends to re-issue the same retrieval while
it thinks. Marking the repeat lets the caller notice it is spinning instead of
silently paying for the same answer again. The cache is per process and holds no
results, only a fingerprint of what was returned.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections import OrderedDict

CAPACITY = 64
_ENV_SWITCH = "WIKI_RETRIEVAL_DEDUP"

_seen: OrderedDict[str, dict] = OrderedDict()


def enabled() -> bool:
    return (os.environ.get(_ENV_SWITCH) or "").strip().lower() != "off"


def fingerprint(scope: str, channels: list[str], query: str) -> str:
    normalized = " ".join(query.split()).lower()
    payload = f"{scope}|{','.join(sorted(channels))}|{normalized}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def lookup(scope: str, channels: list[str], query: str) -> dict | None:
    if not enabled():
        return None
    key = fingerprint(scope, channels, query)
    previous = _seen.get(key)
    if previous is None:
        return None
    _seen.move_to_end(key)
    return dict(previous)


def remember(scope: str, channels: list[str], query: str, summary: dict) -> None:
    if not enabled():
        return
    key = fingerprint(scope, channels, query)
    record = dict(summary)
    record["at"] = time.time()
    record["calls"] = int(_seen.get(key, {}).get("calls", 0)) + 1
    _seen[key] = record
    _seen.move_to_end(key)
    while len(_seen) > CAPACITY:
        _seen.popitem(last=False)


def reset() -> None:
    _seen.clear()
