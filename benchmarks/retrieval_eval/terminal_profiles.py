"""Live OpenCode end-to-end profile gate. Does not mutate saved preferences."""
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
QUESTION = "海尔的 PBC 全称是什么？它包含哪三类目标？"


def run(profile):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    env = {**os.environ, "XDG_CONFIG_HOME": str(ROOT / ".mobilework-state/opencode-config"),
           "OPENCODE_CONFIG": str(ROOT / ".mobilework-state/opencode.runtime.json"),
           "MOBILEWORK_ROOT": str(ROOT)}
    prompt = QUESTION + '\n<mobilework-retrieval>' + json.dumps({"retrieval_profile": profile}) + '</mobilework-retrieval>'
    started = time.monotonic()
    command = [shutil.which("opencode"), "run", "--agent", "mobilework", "--model",
               "openrouter/qwen/qwen3.8-flash", "--format", "json"]
    try:
        process = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                                 input=prompt, encoding="utf-8", errors="replace", timeout=180)
        events = []
        for line in process.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        answers = [e.get("part", {}).get("text", "") for e in events if e.get("type") == "text"]
        calls = [e["part"] for e in events if e.get("type") == "tool_use"]
        answer = "\n".join(answers)
        retrieves = [c for c in calls if c.get("tool", "").endswith("_retrieve")]
        actual_channels = []
        truncated = False
        for call in retrieves:
            state = call.get("state", {})
            truncated |= state.get("metadata", {}).get("truncated", False)
            try:
                data = json.loads(state.get("output", "{}"))
                actual_channels.extend(status.get("channels", []) for status in data.get("kb_status", {}).values())
            except ValueError:
                truncated = True
        expected_channels = ["vector"] if profile == "fast" else ["vector", "keyword"] if profile == "balanced" else ["vector", "keyword", "graph"]
        passed = process.returncode == 0 and bool(retrieves) and bool(actual_channels) and all(sorted(c) == sorted(expected_channels) for c in actual_channels) and all(
            fact in answer for fact in ("业务目标", "员工管理目标", "个人发展目标")) and not truncated
        record = {"profile": profile, "question": QUESTION, "passed": passed,
                  "seconds": round(time.monotonic() - started, 2), "exit_code": process.returncode,
                  "answer": answer, "tool_calls": calls, "actual_channels": actual_channels,
                  "errors": [e for e in events if e.get("type") == "error"],
                  "session_ids": sorted({e["sessionID"] for e in events if "sessionID" in e}),
                  "stderr": process.stderr}
    except subprocess.TimeoutExpired:
        record = {"profile": profile, "passed": False, "error": "180 second process timeout"}
    directory = ROOT / "benchmarks/retrieval_eval/artifacts/terminal-profiles"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{run_id}-{profile}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in record.items() if k not in ("tool_calls", "stderr")}, ensure_ascii=False), flush=True)
    return record["passed"]


if __name__ == "__main__":
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        results = list(pool.map(run, ("fast", "balanced", "reasoning", "research")))
    raise SystemExit(0 if all(results) else 1)
