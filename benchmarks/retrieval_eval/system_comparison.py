"""Run reproducible cross-system retrieval experiments.

The harness executes real adapters only. Missing repositories, stopped services,
invalid responses and tool failures become explicit records; they are never
replaced by synthetic successes. Use ``--list`` to inspect the deterministic
schedule without contacting a model or service.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib import error as urlerror
from urllib import request as urlrequest

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmarks.retrieval_eval import multikb


HERE = Path(__file__).resolve().parent
APP_ROOT = HERE.parents[1]
DEFAULT_MANIFEST = HERE / "experiments.system-comparison.v1.json"
DEFAULT_SCHEMA = HERE / "schemas/system-comparison-record.v1.schema.json"
STATUSES = {"ok", "unavailable", "failed"}
EVALUATOR_MODE = "deterministic_lexical_proxy_v1"


def utc_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("comparison manifest must use schema_version 1")
    if int(payload.get("corpus", {}).get("expected_wiki_pages", 0)) != 50:
        raise ValueError("comparison corpus must declare the agreed 50 Wiki pages")
    experiments = payload.get("experiments")
    if not isinstance(experiments, dict) or not experiments:
        raise ValueError("comparison manifest requires experiments")
    question_ids = {q["id"] for q in multikb.load_questions()}
    system_ids = {s["id"] for s in payload.get("systems", [])}
    for experiment_id, experiment in experiments.items():
        if set(experiment["question_ids"]) - question_ids:
            raise ValueError(f"{experiment_id} references unknown questions")
        if set(experiment["systems"]) - system_ids:
            raise ValueError(f"{experiment_id} references unknown systems")
        if int(experiment["repeats"]) < 1 or not experiment.get("conditions"):
            raise ValueError(f"{experiment_id} has an invalid schedule")
    return payload


@dataclass(frozen=True)
class ComparisonSpec:
    run_id: str
    experiment_id: str
    system_id: str
    question_id: str
    query_variant: str
    condition_id: str
    repeat: int
    seed: int
    mode: str
    condition: dict[str, Any] = field(compare=False)


def make_schedule(manifest: Mapping[str, Any], selected: Sequence[str] | None = None) -> list[ComparisonSpec]:
    selected_ids = set(selected or manifest["experiments"])
    unknown = selected_ids - set(manifest["experiments"])
    if unknown:
        raise ValueError(f"unknown experiments: {', '.join(sorted(unknown))}")
    schedule: list[ComparisonSpec] = []
    for experiment_offset, (experiment_id, experiment) in enumerate(manifest["experiments"].items()):
        if experiment_id not in selected_ids:
            continue
        variants = experiment.get("query_variants", ["standard"])
        for repeat in range(1, int(experiment["repeats"]) + 1):
            for system_offset, system_id in enumerate(experiment["systems"]):
                for condition_offset, condition in enumerate(experiment["conditions"]):
                    only_question = condition.get("only_question")
                    for question_offset, question_id in enumerate(experiment["question_ids"]):
                        if only_question and only_question != question_id:
                            continue
                        for variant_offset, variant in enumerate(variants):
                            run_id = f"{experiment_id}-{system_id}-{condition['id']}-{question_id}-{variant}-r{repeat}"
                            seed = (20260907 + experiment_offset * 1_000_000 + repeat * 100_000
                                    + system_offset * 10_000 + condition_offset * 1_000
                                    + question_offset * 10 + variant_offset)
                            schedule.append(ComparisonSpec(run_id, experiment_id, system_id,
                                question_id, variant, condition["id"], repeat, seed,
                                condition.get("mode", "retrieval"), dict(condition)))
    return schedule


@dataclass
class AdapterOutcome:
    status: str
    results: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    citations: list[str] = field(default_factory=list)
    tool_calls: int = 0
    queried_kb_ids: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    route_detail: Any = None
    degradation_detail: list[str] = field(default_factory=list)
    failure_detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"invalid adapter status: {self.status}")


class SystemAdapter(Protocol):
    system_id: str
    system_name: str
    repository: str
    commit: str
    corpus_wiki_pages: int | None

    def run(self, query: str, question: Mapping[str, Any], spec: ComparisonSpec, top_k: int) -> AdapterOutcome: ...


def _canonical_source(path: str, kb_id: str = "") -> str:
    path = path.replace("\\", "/").lstrip("./")
    return f"{kb_id}:{path}" if kb_id else path


def normalize_results(values: Sequence[Mapping[str, Any]], top_k: int) -> list[dict[str, Any]]:
    rows = []
    for rank, value in enumerate(values[:top_k], 1):
        kb_id = str(value.get("kb_id") or "")
        path = str(value.get("path") or value.get("source") or value.get("file") or "")
        chunks = value.get("matched_chunks") or []
        snippet = value.get("snippet") or value.get("content") or value.get("text") or ""
        if not snippet and chunks:
            snippet = "\n".join(str(chunk.get("text", "")) for chunk in chunks)
        score = value.get("global_score", value.get("fused_score", value.get("score")))
        rows.append({
            "rank": rank,
            "kb_id": kb_id,
            "path": path,
            "title": str(value.get("title") or Path(path).stem),
            "snippet": str(snippet),
            "score": float(score) if isinstance(score, (int, float)) and math.isfinite(float(score)) else None,
            "source": _canonical_source(path, kb_id),
        })
    return rows


def _path_matches(expected: str, actual: str) -> bool:
    expected_path = expected.split(":", 1)[-1].replace("\\", "/").casefold()
    actual_path = actual.split(":", 1)[-1].replace("\\", "/").casefold()
    return expected_path == actual_path or expected_path.endswith("/" + actual_path) or actual_path.endswith("/" + expected_path)


def compute_metrics(question: Mapping[str, Any], results: Sequence[Mapping[str, Any]], *,
                    answer: str = "", citations: Sequence[str] = ()) -> dict[str, float | int | None]:
    expected = list(question["expected_sources"])
    ranks = [int(row["rank"]) for row in results
             if any(_path_matches(source, str(row.get("source", ""))) for source in expected)]
    target_kbs = set(question["target_kbs"])
    irrelevant = [row for row in results if row.get("kb_id") and row.get("kb_id") not in target_kbs]
    cited_hits = sum(any(_path_matches(source, cited) for cited in citations) for source in expected)
    answer_norm = "".join(answer.casefold().split())
    matched = sum("".join(fact.casefold().split()) in answer_norm for fact in question["required_facts"])
    forbidden = any("".join(fact.casefold().split()) in answer_norm for fact in question["forbidden_facts"])
    evidence = "\n".join(str(row.get("snippet", "")) for row in results)
    evidence_norm = "".join(evidence.casefold().split())
    answer_lines = ["".join(line.casefold().split()) for line in answer.splitlines() if line.strip()]
    conflict_cues = ("冲突", "差异", "不一致", "范围", "不可直接", "不能一概而论")
    is_answer_run = bool(answer) or bool(citations)
    return {
        "hit_at_5": float(bool(ranks)),
        "mrr": (1.0 / min(ranks)) if ranks else 0.0,
        "citation_recall": (cited_hits / max(1, len(expected))) if is_answer_run else None,
        "irrelevant_evidence_rate": len(irrelevant) / max(1, len(results)),
        "factual_correctness": ((matched / max(1, len(question["required_facts"]))) if not forbidden else 0.0) if is_answer_run else None,
        "grounded_ratio": (sum(line in evidence_norm for line in answer_lines) / max(1, len(answer_lines))) if is_answer_run else None,
        "conflict_handling": float(not question["conflict_expected"] or any(cue in answer for cue in conflict_cues)) if is_answer_run else None,
        "abstention_score": float((not bool(citations)) == (not question["answerable"])) if is_answer_run else None,
        "stale_fact_error": None,
    }


def _git_commit(path: Path) -> str:
    if not (path / ".git").exists():
        return "unknown"
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, text=True,
            capture_output=True, check=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def count_wiki_pages(path: Path) -> int | None:
    wiki = path / "wiki"
    return len([item for item in wiki.rglob("*.md") if item.name not in ("index.md", "log.md")]) if wiki.is_dir() else None


def find_opencode() -> str | None:
    """Return an executable OpenCode entry point on POSIX and Windows."""
    command = shutil.which("opencode.cmd")
    if command:
        native = Path(command).parent / "node_modules/opencode-ai/bin/opencode.exe"
        if native.is_file():
            return str(native)
    return command or shutil.which("opencode")


class MobileworkAdapter:
    system_id = "mobilework"
    system_name = "Mobilework"
    repository = "local"

    def __init__(self, root: Path, retrieve_fn: Callable[..., dict] | None = None,
                 answer_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
                 model: str = "openrouter/qwen/qwen3.8-flash", answer_timeout: int = 180,
                 retrieval_scope: str = "both", exclude_embedding_latency: bool = False):
        self.root = root.resolve()
        from wiki_retrieval import embedding
        embedding.load_dotenv(self.root)
        self.commit = _git_commit(self.root)
        self.corpus_wiki_pages = sum(len([item for item in path.rglob("*.md") if item.name not in ("index.md", "log.md")])
                                     for path in (self.root / "kb").glob("*/wiki"))
        self._retrieve_fn = retrieve_fn
        self._answer_runner = answer_runner or subprocess.run
        self.model = model
        self.answer_timeout = answer_timeout
        self.retrieval_scope = retrieval_scope
        self.exclude_embedding_latency = exclude_embedding_latency
        self._precomputed_embeddings: dict[str, list[float]] = {}
        self._embedding_precompute_ms = 0.0
        self._answer_unavailable_reason: str | None = None

    def precompute_embeddings(self, queries: Sequence[str]) -> None:
        if not self.exclude_embedding_latency:
            return
        from wiki_retrieval import embedding
        unique = list(dict.fromkeys(str(query) for query in queries))
        vectors, elapsed_ms = embedding.fetch_batch(unique)
        self._precomputed_embeddings = dict(zip(unique, vectors, strict=True))
        self._embedding_precompute_ms = elapsed_ms

    def run(self, query: str, question: Mapping[str, Any], spec: ComparisonSpec, top_k: int) -> AdapterOutcome:
        if spec.mode == "answer":
            if self._answer_unavailable_reason:
                return AdapterOutcome("unavailable", failure_detail=self._answer_unavailable_reason)
            profile = str(spec.condition.get("profile") or "balanced")
            executable = find_opencode()
            if executable is None:
                return AdapterOutcome("unavailable", failure_detail="opencode executable not found")
            env = {**os.environ, "XDG_CONFIG_HOME": str(self.root / ".mobilework-state/opencode-config"),
                   "OPENCODE_CONFIG": str(self.root / ".mobilework-state/opencode.runtime.json"),
                   "MOBILEWORK_ROOT": str(self.root)}
            if os.environ.get("EMBEDDING_API_KEY"):
                env["OPENROUTER_API_KEY"] = os.environ["EMBEDDING_API_KEY"]
            prompt = query + "\n<mobilework-retrieval>" + json.dumps(
                {"retrieval_profile": profile}, ensure_ascii=False) + "</mobilework-retrieval>"
            try:
                completed = self._answer_runner([executable, "run", "--agent", "mobilework", "--model",
                    self.model, "--format", "json"], cwd=self.root, env=env, text=True,
                    encoding="utf-8", errors="replace", capture_output=True, input=prompt,
                    timeout=self.answer_timeout, check=False)
                if completed.returncode != 0:
                    detail = (completed.stderr or completed.stdout)[-2000:]
                    self._answer_unavailable_reason = f"OpenCode preflight failed: {detail}"
                    return AdapterOutcome("failed", failure_detail=detail)
                results, answer, citations, calls = parse_opencode_events(completed.stdout, top_k)
                trace = mobilework_trace_metadata(completed.stdout, profile)
                return AdapterOutcome("ok", results, answer, citations, calls,
                    channels=sorted({channel for group in trace["actual_channels"] for channel in group}),
                    metadata=trace)
            except subprocess.TimeoutExpired:
                self._answer_unavailable_reason = f"OpenCode process timed out after {self.answer_timeout} seconds"
                return AdapterOutcome("failed", failure_detail=self._answer_unavailable_reason)
            except Exception as exc:
                return AdapterOutcome("failed", failure_detail=f"{type(exc).__name__}: {exc}")
        if self._retrieve_fn is None:
            from wiki_retrieval.retrieve import retrieve
            self._retrieve_fn = retrieve
        condition = spec.condition
        channels = list(condition.get("channels") or (["vector", "keyword", "graph"] if spec.mode == "retrieval" else []))
        kb_ids = None
        if condition.get("routing") == "all":
            from wiki_retrieval.federated import registry
            kb_ids = list(registry(self.root))
        elif condition.get("routing") == "gold":
            kb_ids = list(question["target_kbs"])
        try:
            kwargs: dict[str, Any] = {"query": query, "root": self.root, "scope": self.retrieval_scope,
                "top_k": top_k, "include_content": True, "verbose": True}
            if channels:
                kwargs["channels"] = channels
            if kb_ids is not None:
                kwargs["kb_ids"] = kb_ids
            if condition.get("profile"):
                kwargs["profile"] = condition["profile"]
            if condition.get("fault") == "embedding_error" and getattr(self._retrieve_fn, "__module__", "").startswith("wiki_retrieval"):
                from wiki_retrieval import embedding
                original_fetch = embedding.fetch
                try:
                    embedding.fetch = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected embedding error"))
                    response = self._retrieve_fn(**kwargs)
                finally:
                    embedding.fetch = original_fetch
            elif query in self._precomputed_embeddings and getattr(self._retrieve_fn, "__module__", "").startswith("wiki_retrieval"):
                from wiki_retrieval import embedding
                original_fetch = embedding.fetch
                try:
                    embedding.fetch = lambda text: (self._precomputed_embeddings[text], 0.0)
                    response = self._retrieve_fn(**kwargs)
                finally:
                    embedding.fetch = original_fetch
            else:
                response = self._retrieve_fn(**kwargs)
            errors = response.get("errors", {})
            degradation = [f"{kb}: {detail}" for kb, detail in errors.items()]
            for kb_id, status in response.get("kb_status", {}).items():
                embedding = status.get("embedding", {}) if isinstance(status, dict) else {}
                if "vector" in channels and embedding.get("status") not in (None, "ok", "success"):
                    degradation.append(f"{kb_id} embedding: {embedding.get('status')}: {embedding.get('error', '')}")
            results = normalize_results(response.get("results", []), top_k)
            required_vector_failed = "vector" in channels and bool(degradation) and condition.get("fault") != "embedding_error"
            failure_detail = "; ".join(degradation) if required_vector_failed else ""
            return AdapterOutcome("failed" if required_vector_failed else "ok", results, "", [], 1,
                list(response.get("queried_kb_ids", [])), channels,
                response.get("routing"), degradation, failure_detail,
                metadata={"partial_failure": bool(errors) or bool(degradation),
                    "latency_basis": "network_excluded" if query in self._precomputed_embeddings else "end_to_end",
                    "retrieval_scope": self.retrieval_scope,
                    "embedding_precompute_batch_ms": self._embedding_precompute_ms if query in self._precomputed_embeddings else None,
                    "timings": response.get("timings"), "kb_status": response.get("kb_status")})
        except Exception as exc:
            return AdapterOutcome("failed", failure_detail=f"{type(exc).__name__}: {exc}", channels=channels)


class NashsuHttpAdapter:
    system_id = "nashsu"
    system_name = "nashsu/llm_wiki"
    repository = "https://github.com/nashsu/llm_wiki"

    def __init__(self, base_url: str, project_id: str = "current", repository_path: Path | None = None,
                 request_fn: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
                 corpus_wiki_pages: int | None = None):
        self.base_url = base_url.rstrip("/")
        self.project_id = project_id
        self.repository_path = repository_path
        self.commit = _git_commit(repository_path) if repository_path else "unknown"
        self.corpus_wiki_pages = corpus_wiki_pages
        self._request_fn = request_fn or self._post

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("LLM_WIKI_API_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urlrequest.Request(self.base_url + path, data=body,
            headers=headers, method="POST")
        with urlrequest.urlopen(req, timeout=120) as response:
            value = json.loads(response.read().decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("nashsu API returned a non-object response")
        return value

    def run(self, query: str, question: Mapping[str, Any], spec: ComparisonSpec, top_k: int) -> AdapterOutcome:
        path = f"/api/v1/projects/{self.project_id}/{'chat' if spec.mode == 'answer' else 'search'}"
        payload = ({"message": query, "topK": top_k, "includeContent": True,
                    "persistSession": False, "tools": {"wiki": True, "web": False, "anytxt": False}}
                   if spec.mode == "answer" else {"query": query, "topK": top_k, "includeContent": True})
        try:
            response = self._request_fn(path, payload)
            if response.get("ok") is False:
                raise RuntimeError(str(response.get("error") or "nashsu API rejected request"))
            values = response.get("references", []) if spec.mode == "answer" else response.get("results", [])
            results = normalize_results(values, top_k)
            message = response.get("message", {})
            answer = str(message.get("content", "")) if isinstance(message, dict) else str(message or "")
            citations = [_canonical_source(str(item.get("path", ""))) for item in response.get("references", [])]
            tool_events = response.get("toolEvents", [])
            return AdapterOutcome("ok", results, answer, citations,
                len([e for e in tool_events if e.get("status") in ("start", "started")]) or 1,
                channels=[str(response.get("mode", "native_hybrid"))], metadata={
                    "token_hits": response.get("tokenHits"), "vector_hits": response.get("vectorHits"),
                    "graph_hits": response.get("graphHits")})
        except (urlerror.URLError, TimeoutError, ConnectionError) as exc:
            return AdapterOutcome("unavailable", failure_detail=f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            return AdapterOutcome("failed", failure_detail=f"{type(exc).__name__}: {exc}")


def parse_opencode_events(output: str, top_k: int) -> tuple[list[dict[str, Any]], str, list[str], int]:
    values, texts, tool_calls = [], [], 0
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type", ""))
        part = event.get("part") if isinstance(event.get("part"), dict) else event
        if event_type in ("tool_use", "tool") or part.get("type") in ("tool", "tool_use"):
            tool_calls += 1
        text = part.get("text") or event.get("text")
        if isinstance(text, str):
            texts.append(text)
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        candidate = part.get("output") or state.get("output") or event.get("output")
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except json.JSONDecodeError:
                candidate = None
        if isinstance(candidate, dict) and isinstance(candidate.get("results"), list):
            values.extend(candidate["results"])
    answer = "\n".join(texts)
    if not values:
        paths = list(dict.fromkeys(re.findall(r"(?:kb[/\\][^/\\\s]+[/\\])?wiki[/\\][^\s\]\)>'\"]+?\.md", answer, re.IGNORECASE)))
        values = [{"path": path, "title": Path(path).stem} for path in paths]
    results = normalize_results(values, top_k)
    citations = [row["source"] for row in results]
    return results, answer, citations, tool_calls


def mobilework_trace_metadata(output: str, profile: str) -> dict[str, Any]:
    actual_channels: list[list[str]] = []
    stop_reasons: list[str] = []
    retrieval_calls = 0
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = event.get("part", {}) if isinstance(event, dict) else {}
        if event.get("type") != "tool_use" or not str(part.get("tool", "")).endswith("_retrieve"):
            continue
        retrieval_calls += 1
        state = part.get("state", {}) if isinstance(part.get("state"), dict) else {}
        raw = state.get("output", "")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        for status in payload.get("kb_status", {}).values():
            if isinstance(status, dict) and isinstance(status.get("channels"), list):
                actual_channels.append([str(value) for value in status["channels"]])
        if payload.get("stop_reason"):
            stop_reasons.append(str(payload["stop_reason"]))
    expected = (["vector"] if profile == "fast" else ["vector", "keyword"]
                if profile == "balanced" else ["vector", "keyword", "graph"])
    limit = {"fast": 1, "balanced": 1, "reasoning": 4, "research": 8}.get(profile, 0)
    return {"profile": profile, "retrieval_calls": retrieval_calls,
            "actual_channels": actual_channels, "expected_channels": expected,
            "max_tool_calls": limit, "stop_reasons": stop_reasons,
            "profile_constraints_passed": bool(actual_channels)
                and retrieval_calls <= limit
                and all(sorted(value) == sorted(expected) for value in actual_channels)}


class KarpathyOpenCodeAdapter:
    system_id = "karpathy"
    system_name = "Astro-Han/karpathy-llm-wiki"
    repository = "https://github.com/Astro-Han/karpathy-llm-wiki"

    def __init__(self, repository_path: Path, command: Sequence[str] | None = None,
                 model: str = "openrouter/qwen/qwen3.8-flash",
                 runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
                 timeout: int = 180):
        self.repository_path = repository_path.resolve()
        self.command = list(command or ([find_opencode(), "run"] if find_opencode() else ["opencode", "run"]))
        self.model = model
        self.commit = _git_commit(self.repository_path)
        self.corpus_wiki_pages = count_wiki_pages(self.repository_path)
        self._runner = runner or subprocess.run
        self.timeout = timeout
        self._unavailable_reason: str | None = None

    def run(self, query: str, question: Mapping[str, Any], spec: ComparisonSpec, top_k: int) -> AdapterOutcome:
        if self._unavailable_reason:
            return AdapterOutcome("unavailable", failure_detail=self._unavailable_reason)
        if not self.repository_path.is_dir():
            return AdapterOutcome("unavailable", failure_detail=f"repository not found: {self.repository_path}")
        skill = self.repository_path / "SKILL.md"
        if not skill.is_file() and not any(self.repository_path.glob("**/SKILL.md")):
            return AdapterOutcome("unavailable", failure_detail="Karpathy SKILL.md not found")
        prompt = (f"Use the repository's LLM Wiki skill to answer this question from its Wiki only. "
                  f"Return at most {top_k} sources with their paths. Question: {query}")
        try:
            env = dict(os.environ)
            if os.environ.get("EMBEDDING_API_KEY"):
                env["OPENROUTER_API_KEY"] = os.environ["EMBEDDING_API_KEY"]
            completed = self._runner([*self.command, "--format", "json", "--model", self.model, prompt],
                cwd=self.repository_path, text=True, encoding="utf-8", errors="replace",
                env=env, capture_output=True, timeout=self.timeout, check=False)
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout)[-2000:]
                self._unavailable_reason = f"OpenCode preflight failed: {detail}"
                return AdapterOutcome("failed", failure_detail=detail)
            results, answer, citations, calls = parse_opencode_events(completed.stdout, top_k)
            return AdapterOutcome("ok", results, answer if spec.mode == "answer" else "",
                citations if spec.mode == "answer" else [], calls)
        except FileNotFoundError as exc:
            self._unavailable_reason = f"{type(exc).__name__}: {exc}"
            return AdapterOutcome("unavailable", failure_detail=self._unavailable_reason)
        except subprocess.TimeoutExpired:
            self._unavailable_reason = f"OpenCode process timed out after {self.timeout} seconds"
            return AdapterOutcome("failed", failure_detail=self._unavailable_reason)
        except Exception as exc:
            return AdapterOutcome("failed", failure_detail=f"{type(exc).__name__}: {exc}")


def run_spec(adapter: SystemAdapter, question: Mapping[str, Any], spec: ComparisonSpec,
             query: str, top_k: int, provenance_label: str = "current_run") -> dict[str, Any]:
    started_at, started = utc_stamp(), time.perf_counter()
    if adapter.corpus_wiki_pages != 50:
        detail = ("corpus page count is unknown; supply a verified count before comparison"
                  if adapter.corpus_wiki_pages is None else
                  f"corpus has {adapter.corpus_wiki_pages} Wiki content pages; expected 50")
        outcome = AdapterOutcome("unavailable", failure_detail=detail,
            metadata={"corpus_wiki_pages": adapter.corpus_wiki_pages})
    else:
        outcome = adapter.run(query, question, spec, top_k)
    latency_ms = (time.perf_counter() - started) * 1000
    record = {
        "schema_version": 1, "record_type": "system_comparison_run", **asdict(spec),
        "system_name": adapter.system_name, "system_commit": adapter.commit,
        "repository": adapter.repository, "provenance_label": provenance_label,
        "provenance_origin": spec.run_id, "query": query, "status": outcome.status,
        "started_at": started_at, "finished_at": utc_stamp(), "latency_ms": latency_ms,
        "tool_calls": outcome.tool_calls, "results": outcome.results, "answer": outcome.answer,
        "citations": outcome.citations, "queried_kb_ids": outcome.queried_kb_ids,
        "channels": outcome.channels, "route_detail": outcome.route_detail,
        "degradation_detail": outcome.degradation_detail, "failure_detail": outcome.failure_detail,
        "metrics": compute_metrics(question, outcome.results, answer=outcome.answer,
                                   citations=outcome.citations), "adapter_metadata": outcome.metadata,
        "evaluator_mode": EVALUATOR_MODE,
    }
    validate_comparison_record(record)
    return record


def validate_comparison_record(record: Mapping[str, Any]) -> None:
    required = {"schema_version", "record_type", "run_id", "experiment_id", "system_id",
        "system_name", "system_commit", "provenance_label", "question_id", "query",
        "query_variant", "condition_id", "repeat", "seed", "mode", "status",
        "started_at", "finished_at", "latency_ms", "tool_calls", "results", "answer",
        "citations", "failure_detail", "metrics"}
    if record.get("schema_version") != 1 or record.get("record_type") != "system_comparison_run" or required - set(record):
        raise ValueError("invalid system comparison record")
    if record["status"] not in STATUSES:
        raise ValueError("invalid comparison status")
    if record["provenance_label"] not in ("current_run", "historical_result"):
        raise ValueError("invalid provenance label")
    if not isinstance(record["results"], list) or not isinstance(record["metrics"], dict):
        raise ValueError("comparison results and metrics must be objects")


def aggregate_runs(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = {}
    for row in records:
        key = (str(row["experiment_id"]), str(row["system_id"]),
               str(row["condition_id"]), str(row["query_variant"]))
        groups.setdefault(key, []).append(row)
    result = []
    for key, rows in sorted(groups.items()):
        ok = [row for row in rows if row["status"] == "ok"]
        numeric = lambda name: [float(row["metrics"][name]) for row in ok if row["metrics"].get(name) is not None]
        latencies = sorted(float(row["latency_ms"]) for row in ok)
        result.append({
            "experiment_id": key[0], "system_id": key[1], "condition_id": key[2], "query_variant": key[3],
            "run_count": len(rows), "ok_count": len(ok), "failure_count": len(rows) - len(ok),
            "hit_at_5": sum(numeric("hit_at_5")) / len(numeric("hit_at_5")) if numeric("hit_at_5") else None,
            "mrr": sum(numeric("mrr")) / len(numeric("mrr")) if numeric("mrr") else None,
            "citation_recall": sum(numeric("citation_recall")) / len(numeric("citation_recall")) if numeric("citation_recall") else None,
            "irrelevant_evidence_rate": sum(numeric("irrelevant_evidence_rate")) / len(numeric("irrelevant_evidence_rate")) if numeric("irrelevant_evidence_rate") else None,
            "p50_latency_ms": latencies[(len(latencies) - 1) // 2] if latencies else None,
            "p95_latency_ms": latencies[max(0, math.ceil(.95 * len(latencies)) - 1)] if latencies else None,
            "mean_tool_calls": sum(float(row["tool_calls"]) for row in ok) / len(ok) if ok else None,
        })
    return result


def import_historical_report(report_path: Path, provenance_id: str | None = None) -> list[dict[str, Any]]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    summary = report.get("run_summary", {})
    scores = {row["run_id"]: row for row in report.get("answer_scores", [])}
    questions = {row["id"]: row for row in report.get("questions", multikb.load_questions())}
    imported = []
    for run in report.get("retrieval_runs", []):
        question = questions[run["question_id"]]
        score = scores.get(run["run_id"], {})
        normalized = normalize_results(run.get("results", []), 5)
        metrics = compute_metrics(question, normalized, answer=str(score.get("answer", "")), citations=score.get("citations", []))
        for name in metrics:
            if name in score and (name != "stale_fact_error" or score.get("stale_fact_error_measured")):
                metrics[name] = score[name]
        status = "ok" if run.get("status") == "ok" else "failed"
        record = {
            "schema_version": 1, "record_type": "system_comparison_run", "run_id": f"historical-{run['run_id']}",
            "experiment_id": "historical_multikb", "system_id": "mobilework", "system_name": "Mobilework",
            "system_commit": str(summary.get("git_commit", "unknown")), "repository": "local",
            "provenance_label": "historical_result", "provenance_origin": provenance_id or summary.get("run_id", report_path.parent.name),
            "question_id": run["question_id"], "query": question["question"], "query_variant": "standard",
            "condition_id": run["config_id"], "repeat": run["repeat"], "seed": run["seed"],
            "mode": "retrieval", "condition": None, "status": status,
            "started_at": run.get("started_at", summary.get("started_at", utc_stamp())),
            "finished_at": run.get("finished_at", summary.get("finished_at", utc_stamp())),
            "latency_ms": float(run.get("latency_ms", 0)), "tool_calls": int(run.get("tool_calls", 0)),
            "results": normalized, "answer": str(score.get("answer", "")), "citations": score.get("citations", []),
            "queried_kb_ids": run.get("kb_ids", []), "channels": [], "route_detail": run.get("route_reason"),
            "degradation_detail": run.get("degradations", []), "failure_detail": run.get("failure_detail", ""),
            "metrics": metrics, "adapter_metadata": {"evaluator_mode": summary.get("evaluator_mode")},
            "evaluator_mode": str(summary.get("evaluator_mode") or EVALUATOR_MODE),
        }
        validate_comparison_record(record)
        imported.append(record)
    return imported


def import_terminal_profiles(path: Path) -> list[dict[str, Any]]:
    """Import pre-refactor OpenCode profile snapshots as historical answer rows."""
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    question = next(row for row in multikb.load_questions() if row["id"] == "Q01")
    imported = []
    for offset, item in enumerate(files):
        payload = json.loads(item.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or "profile" not in payload:
            continue
        tool_calls = payload.get("tool_calls", [])
        event_lines = []
        for call in tool_calls if isinstance(tool_calls, list) else []:
            event_lines.append(json.dumps({"type": "tool_use", "part": call}, ensure_ascii=False))
        results, _, citations, calls = parse_opencode_events("\n".join(event_lines), 5)
        answer = str(payload.get("answer", ""))
        status = "ok" if payload.get("passed") is True else "failed"
        began = str(payload.get("started_at") or "2026-09-04T00:00:00+00:00")
        channels = []
        for group in payload.get("actual_channels", []):
            channels.extend(str(value) for value in group) if isinstance(group, list) else channels.append(str(group))
        record = {
            "schema_version": 1, "record_type": "system_comparison_run", "run_id": f"historical-terminal-{item.stem}",
            "experiment_id": "historical_skill_regression", "system_id": "mobilework", "system_name": "Mobilework",
            "system_commit": str(payload.get("git_commit", "unknown")), "repository": "local",
            "provenance_label": "historical_result", "provenance_origin": path.name,
            "question_id": "Q01", "query": str(payload.get("question", question["question"])), "query_variant": "standard",
            "condition_id": str(payload["profile"]), "repeat": 1, "seed": 20260904 + offset,
            "mode": "answer", "condition": {"profile": payload["profile"]}, "status": status,
            "started_at": began, "finished_at": str(payload.get("finished_at") or began),
            "latency_ms": float(payload.get("seconds", 0)) * 1000, "tool_calls": calls,
            "results": results, "answer": answer, "citations": citations,
            "queried_kb_ids": [], "channels": channels,
            "route_detail": None, "degradation_detail": [],
            "failure_detail": str(payload.get("error") or ("; ".join(map(str, payload.get("errors", []))) if status != "ok" else "")),
            "metrics": compute_metrics(question, results, answer=answer, citations=citations),
            "adapter_metadata": {"legacy_passed": payload.get("passed")},
            "evaluator_mode": EVALUATOR_MODE,
        }
        validate_comparison_record(record)
        imported.append(record)
    return imported


def import_historical_path(path: Path) -> list[dict[str, Any]]:
    candidate = path / "report.json" if path.is_dir() and (path / "report.json").is_file() else path
    if candidate.is_file() and candidate.name == "report.json":
        return import_historical_report(candidate, provenance_id=path.name)
    if path.is_dir() or candidate.is_file():
        return import_terminal_profiles(path)
    raise FileNotFoundError(path)


def apply_stale_fact_labels(records: Sequence[dict[str, Any]], label_path: Path) -> int:
    payload = json.loads(label_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("labels"), dict):
        raise ValueError("stale-fact labels must use schema_version 1")
    applied = 0
    for record in records:
        if payload.get("provenance_id") and record.get("provenance_origin") != payload["provenance_id"]:
            continue
        question = payload["labels"].get(record.get("question_id"), {})
        value = question.get(record.get("condition_id")) if isinstance(question, dict) else None
        if value in (0, 1):
            record["metrics"]["stale_fact_error"] = int(value)
            record.setdefault("adapter_metadata", {})["stale_fact_label"] = {
                "source": label_path.name,
                "reviewed_at": payload.get("reviewed_at"),
                "rationale": question.get("rationale", ""),
            }
            applied += 1
    return applied


def default_adapters(root: Path, args: argparse.Namespace) -> dict[str, SystemAdapter]:
    baseline_root = Path(args.baseline_root).resolve()
    return {
        "mobilework": MobileworkAdapter(root, model=args.model, answer_timeout=args.opencode_timeout,
            retrieval_scope=args.mobilework_scope,
            exclude_embedding_latency=args.mobilework_exclude_embedding_latency),
        "nashsu": NashsuHttpAdapter(args.nashsu_url, args.nashsu_project,
            baseline_root / "nashsu-llm-wiki", corpus_wiki_pages=args.nashsu_wiki_pages),
        "karpathy": KarpathyOpenCodeAdapter(baseline_root / "karpathy-llm-wiki", model=args.model,
                                             timeout=args.opencode_timeout),
    }


def write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    raw_path = path.parent / "raw_runs.jsonl"
    raw_path.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in report["raw_runs"]) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=APP_ROOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=HERE / "artifacts" / f"system-comparison-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
    parser.add_argument("--experiment", action="append", dest="experiments")
    parser.add_argument("--system", action="append", dest="systems")
    parser.add_argument("--run-id", action="append", dest="run_ids")
    parser.add_argument("--baseline-root", type=Path, default=HERE / "artifacts/baselines")
    parser.add_argument("--nashsu-url", default="http://127.0.0.1:19828")
    parser.add_argument("--nashsu-project", default="current")
    parser.add_argument("--nashsu-wiki-pages", type=int)
    parser.add_argument("--model", default="openrouter/qwen/qwen3.8-flash")
    parser.add_argument("--opencode-timeout", type=int, default=180)
    parser.add_argument("--mobilework-scope", choices=("wiki", "source", "both"), default="both")
    parser.add_argument("--mobilework-exclude-embedding-latency", action="store_true",
        help="precompute query embeddings before timed runs and reuse them across knowledge bases")
    parser.add_argument("--import-historical", type=Path, action="append", default=[])
    parser.add_argument("--stale-labels", type=Path)
    parser.add_argument("--list", action="store_true", help="print the schedule without running adapters")
    args = parser.parse_args(argv)
    manifest = load_manifest(args.manifest)
    schedule = make_schedule(manifest, args.experiments)
    if args.systems:
        wanted = set(args.systems)
        schedule = [spec for spec in schedule if spec.system_id in wanted]
    if args.run_ids:
        wanted_runs = set(args.run_ids)
        schedule = [spec for spec in schedule if spec.run_id in wanted_runs]
    if args.list:
        print(json.dumps({"run_count": len(schedule), "runs": [asdict(spec) for spec in schedule]}, ensure_ascii=False, indent=2))
        return 0
    adapters = default_adapters(args.root.resolve(), args)
    questions = {q["id"]: q for q in multikb.load_questions()}
    fuzzy = manifest.get("fuzzy_queries", {})
    top_k = int(manifest["corpus"]["top_k"])
    mobilework = adapters.get("mobilework")
    if isinstance(mobilework, MobileworkAdapter) and mobilework.exclude_embedding_latency:
        timed_queries = []
        for spec in schedule:
            if spec.system_id != "mobilework" or spec.mode != "retrieval" or spec.condition.get("fault") == "embedding_error":
                continue
            timed_queries.append(fuzzy.get(spec.question_id, questions[spec.question_id]["question"])
                if spec.query_variant == "fuzzy" else questions[spec.question_id]["question"])
        mobilework.precompute_embeddings(timed_queries)
    records = []
    for spec in schedule:
        query = fuzzy.get(spec.question_id, questions[spec.question_id]["question"]) if spec.query_variant == "fuzzy" else questions[spec.question_id]["question"]
        records.append(run_spec(adapters[spec.system_id], questions[spec.question_id], spec, query, top_k))
        print(f"completed {len(records)}/{len(schedule)}: {spec.run_id} ({records[-1]['status']})", flush=True)
    historical_imports = []
    for historical in args.import_historical:
        imported = import_historical_path(historical)
        records.extend(imported)
        historical_imports.append({"path": str(historical), "record_count": len(imported), "provenance_label": "historical_result"})
    stale_labels_applied = apply_stale_fact_labels(records, args.stale_labels) if args.stale_labels else 0
    systems = [{**system, "commit": adapters[system["id"]].commit,
                "corpus_wiki_pages": adapters[system["id"]].corpus_wiki_pages}
               for system in manifest["systems"]]
    report = {"schema_version": 1, "run_summary": {"run_id": args.output_dir.name,
        "started_at": records[0]["started_at"] if records else utc_stamp(), "finished_at": utc_stamp(),
        "run_count": len(records), "current_run_count": sum(r["provenance_label"] == "current_run" for r in records),
        "historical_run_count": sum(r["provenance_label"] == "historical_result" for r in records),
        "evaluator_mode": EVALUATOR_MODE,
        "note": "Unavailable and failed baselines are recorded; no result is synthesized. Answer metrics are conservative lexical proxies, not human judgments."},
        "systems": systems, "questions": list(questions.values()), "experiment_manifest": manifest,
        "stale_labels": {"path": str(args.stale_labels) if args.stale_labels else None,
                         "applied_count": stale_labels_applied},
        "raw_runs": records, "aggregate": aggregate_runs(records),
        "failures": [{key: row[key] for key in ("run_id", "experiment_id", "system_id", "question_id", "condition_id", "status", "failure_detail")}
                     for row in records if row["status"] != "ok"], "historical_imports": historical_imports}
    write_report(args.output_dir / "comparison-report.json", report)
    print(json.dumps({"report": str(args.output_dir / "comparison-report.json"), **report["run_summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
