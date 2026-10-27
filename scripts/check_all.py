"""Unified check runner — one command for every cheap check, exit 1 on failure.

Layer-0 test infra. Aggregates the checks that used to live only in session
memory into a single entry point; run it after any non-trivial change.

  python scripts/check_all.py [--only module-tests|undefined-names|registry|fake-provider|fixtures|frontend-js]

Checks:
  module-tests    scripts/test_model_profiles.py / test_provider_store.py /
                  test_usage_log.py / test_llm_json.py (assert scripts)
  undefined-names whole-repo static scan: every name used but never bound in
                  its module. Catches the two shapes nothing else sees — an
                  undefined *annotation* name (frozen by
                  `from __future__ import annotations`, so it only ever shows
                  as an IDE squiggle) and an undefined name in a *function
                  body* (a real NameError, but only on the branch that runs).
                  No mypy/pyright config exists, and runtime suites can't
                  reach it: `logger` sat undefined in runtime.py's graph-
                  evolution path for months because only the success branch
                  touched it, inside a try that swallowed the first throw.
  registry        create_builtin_registry() boots; core tools resolvable
  fake-provider   fake_provider.py boots on a free port; chat (plain + SSE),
                  embeddings, models, scenario rules and fault injection
                  all behave — live smoke of the Layer-1 mock
  fixtures        migration drill: current code opens every fixture copy
                  (data/fixtures/*, snapshot by make_testbed.py
                  --capture-fixtures) and row counts must match the
                  manifest — SKIP when no baseline captured yet
  frontend-js     node --check on every reader JS re-parsed as ESM (.mjs
                  copy trick — CJS check misses ESM-semantics breakage)
"""

from __future__ import annotations

import ast
import builtins
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parent.parent
MODULE_TESTS = ["test_model_profiles.py", "test_provider_store.py",
                "test_usage_log.py", "test_llm_json.py",
                "test_graph_invariants.py", "test_graph_recorder.py",
                "test_graph_selection.py", "test_graph_scope.py",
                "test_graph_orchestrator.py", "test_chunk_reuse.py",
                "test_graph_llm_budget.py", "test_graph_e2e.py",
                "test_author_graph.py", "test_author_enrich_e2e.py",
                "test_author_sources.py", "test_author_voice.py",
                "test_author_dossier.py",
                "test_web_search_service.py",
                "test_writegraph.py", "test_writegraph_vcs.py",
                "test_writegraph_git_ops.py", "test_writing_voice.py",
                "test_writing_git_tools.py", "test_chat_mode_context.py",
                "test_chat_context.py"]


@dataclass
class CheckResult:
    name: str
    status: str          # PASS | FAIL | SKIP
    detail: str = ""
    seconds: float = 0.0


def _run(cmd: list[str], timeout: float = 180,
         extra_env: dict | None = None) -> tuple[int, str]:
    """Run a subprocess, return (returncode, tail of combined output)."""
    env = dict(os.environ, **(extra_env or {}))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env=env)
    except subprocess.TimeoutExpired:
        return 1, f"TIMEOUT after {timeout}s"
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode, out.strip()[-2000:]


def check_module_tests() -> CheckResult:
    t0 = time.monotonic()
    fails: list[str] = []
    for name in MODULE_TESTS:
        code, out = _run([sys.executable, str(ROOT / "scripts" / name)])
        if code != 0:
            fails.append(f"{name}:\n{out}")
    detail = "\n".join(fails)
    return CheckResult("module-tests", "FAIL" if fails else "PASS",
                       detail, time.monotonic() - t0)


# ── undefined-names ───────────────────────────────────────────────────────
# 判据刻意宽松：名字只要在文件**任何地方**绑定过就算数（哪怕在别的函数、
# 别的分支里）。宁可漏报也不误报 —— 这个检查只有恒绿才有价值，一旦出现
# 假阳性就会被绕过。它抓的是「本模块根本没这个名字」这一类。

# Python 在模块里自动提供的名字，不在 builtins 里但随时可用。
_MODULE_GLOBALS = frozenset({
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__annotations__", "__dict__",
    "__path__", "__debug__", "__class__",
})

# 非项目代码 / 用户数据：扫它们只会制造噪音。
_SKIP_DIRS = frozenset({
    "__pycache__", ".git", ".venv", "venv", ".codegraph", ".claude",
    "node_modules", "data",
})


def _module_bindings(tree: ast.AST) -> set[str]:
    """模块里出现过的所有绑定名（宽松）。"""
    names: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names |= {(a.asname or a.name).split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            names |= {a.asname or a.name for a in n.names}
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            names |= set(n.names)
        elif isinstance(n, ast.MatchAs) and n.name:
            names.add(n.name)          # case x:
        elif isinstance(n, ast.MatchStar) and n.name:
            names.add(n.name)          # case [*rest]:
        elif isinstance(n, (ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple)):
            names.add(n.name)          # PEP 695: def f[T]()
    return names


def check_undefined_names() -> CheckResult:
    """Whole-repo: names that are loaded but never bound in their module."""
    t0 = time.monotonic()
    hits: list[str] = []
    scanned = 0
    for path in sorted(ROOT.rglob("*.py")):
        if _SKIP_DIRS & set(path.parts):
            continue
        rel = path.relative_to(ROOT)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            hits.append(f"{rel}: unparseable — {exc}")
            continue
        scanned += 1
        have = _module_bindings(tree) | set(dir(builtins)) | _MODULE_GLOBALS
        seen: dict[str, int] = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                if n.id not in have:
                    seen.setdefault(n.id, n.lineno)
        for name, line in sorted(seen.items(), key=lambda kv: kv[1]):
            hits.append(f"{rel}:{line}: {name}")
    return CheckResult("undefined-names", "FAIL" if hits else "PASS",
                       "\n".join(hits) or f"{scanned} files clean",
                       time.monotonic() - t0)


def check_registry() -> CheckResult:
    t0 = time.monotonic()
    code = (
        "import sys; sys.path.insert(0, r'{root}'); "
        "from tools import create_builtin_registry; "
        "reg = create_builtin_registry(); "
        "core = ['read_file', 'write_file', 'get_time', 'list_directory', "
        "'get_weather']; "
        "missing = [n for n in core if reg.get(n) is None]; "
        "assert not missing, 'missing tools: ' + repr(missing); "
        "print('registry ok')".format(root=str(ROOT))
    )
    rc, out = _run([sys.executable, "-c", code])
    return CheckResult("registry", "PASS" if rc == 0 else "FAIL",
                       "" if rc == 0 else out, time.monotonic() - t0)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http(url: str, payload: dict | None = None) -> tuple[int, str]:
    """POST json (GET when payload None); returns (status, body) incl. 4xx/5xx."""
    try:
        if payload is None:
            with urllib.request.urlopen(url, timeout=15) as r:
                return r.status, r.read().decode("utf-8", "replace")
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _chat(base: str, content: str, *, stream: bool = False) -> tuple[int, str]:
    payload: dict = {"model": "fake-model", "stream": stream,
                     "messages": [{"role": "user", "content": content}]}
    if stream:
        payload["stream_options"] = {"include_usage": True}
    return _http(f"{base}/v1/chat/completions", payload)


def check_fake_provider() -> CheckResult:
    t0 = time.monotonic()
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "fake_provider.py"),
         "--port", str(port)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    problems: list[str] = []
    try:
        deadline = time.monotonic() + 10
        while True:                       # wait for boot
            try:
                _http(f"{base}/_mock/requests")
                break
            except Exception:
                if time.monotonic() > deadline:
                    return CheckResult("fake-provider", "FAIL",
                                       "server did not boot",
                                       time.monotonic() - t0)
                time.sleep(0.15)

        def want(cond: bool, msg: str) -> None:
            if not cond:
                problems.append(msg)

        st, body = _http(f"{base}/v1/models")
        want(st == 200 and "fake-model" in body, f"models: {st} {body[:80]}")

        st, body = _chat(base, "你好")
        data = json.loads(body) if st == 200 else {}
        want(st == 200 and "【fake】" in data.get("choices", [{}])[0]
             .get("message", {}).get("content", ""), f"chat plain: {st} {body[:120]}")

        st, body = _chat(base, "流式测试", stream=True)
        lines = [ln[6:] for ln in body.splitlines() if ln.startswith("data: ")]
        want(st == 200 and "data: [DONE]" in body and lines,
             f"chat stream missing frames: {st}")
        chunks = [json.loads(x) for x in lines if x and x != "[DONE]"]
        want(any(c.get("usage") for c in chunks), "stream: no usage chunk")
        want(any(c.get("choices") and c["choices"][0]["delta"].get("content")
                 for c in chunks), "stream: no content delta")

        st, body = _http(f"{base}/v1/embeddings", {"model": "e", "input": ["甲", "乙"]})
        emb = json.loads(body)["data"] if st == 200 else []
        want(st == 200 and len(emb) == 2 and len(emb[0]["embedding"]) == 64,
             f"embeddings: {st}")
        _, body2 = _http(f"{base}/v1/embeddings", {"model": "e", "input": ["甲"]})
        v1 = json.loads(body2)["data"][0]["embedding"]
        want(v1 == emb[0]["embedding"], "embeddings not deterministic")

        rules = {"rules": [
            {"contains": "抽卡", "response": {"text": "SCEN_OK"}},
            {"contains": "用工具",
             "response": {"tool_calls": [{"name": "read_book",
                                          "arguments": {"query": "x"}}]}}]}
        _http(f"{base}/_mock/scenario", rules)
        _, body = _chat(base, "帮我抽卡")
        want(json.loads(body)["choices"][0]["message"]["content"] == "SCEN_OK",
             f"scenario text rule: {body[:120]}")
        _, body = _chat(base, "请用工具查书")
        msg = json.loads(body)["choices"][0]["message"]
        want(msg.get("tool_calls", [{}])[0].get("function", {}).get("name")
             == "read_book", f"scenario tool rule: {body[:160]}")

        _http(f"{base}/_mock/fault", {"mode": "http_500", "times": 1})
        st, _ = _chat(base, "这次该 500 了")
        want(st == 500, f"fault http_500: got {st}")
        st, _ = _chat(base, "500 已消耗，恢复正常")
        want(st == 200, f"fault not consumed: got {st}")

        _http(f"{base}/_mock/fault", {"mode": "empty_stream", "times": 1})
        st, body = _chat(base, "空响应", stream=True)
        deltas = [ln for ln in body.splitlines()
                  if ln.startswith("data: ") and "[DONE]" not in ln
                  and json.loads(ln[6:]).get("choices")]
        want(st == 200 and not deltas, f"empty_stream leaked content: {body[:120]}")

        st, body = _http(f"{base}/_mock/requests")
        n_req = len(json.loads(body)["requests"])
        want(n_req >= 5, f"request log too small: {n_req}")
        _http(f"{base}/_mock/reset")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    return CheckResult("fake-provider", "FAIL" if problems else "PASS",
                       "; ".join(problems)[:2000], time.monotonic() - t0)


def check_frontend_js() -> CheckResult:
    """node --check every reader JS as ESM (.mjs copy trick)."""
    t0 = time.monotonic()
    node = shutil.which("node")
    if not node:
        return CheckResult("frontend-js", "SKIP", "node not on PATH")
    files = sorted((ROOT / "reader").glob("*.js")) + \
        sorted((ROOT / "reader" / "js").glob("*.js"))
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        for f in files:
            mjs = Path(td) / (f.stem + ".mjs")
            shutil.copy(f, mjs)
            rc, out = _run([node, "--check", str(mjs)], timeout=30)
            if rc != 0:
                tail = out.strip().splitlines()[-1] if out.strip() else "syntax error"
                fails.append(f"{f.name}: {tail}")
    detail = "\n".join(fails)
    return CheckResult("frontend-js", "FAIL" if fails else "PASS", detail,
                       time.monotonic() - t0)


def check_fixtures() -> CheckResult:
    """Migration drill — delegate to make_testbed.py --verify-fixtures."""
    t0 = time.monotonic()
    fdir = ROOT / "data" / "fixtures"
    has_base = False
    if fdir.exists():
        has_base = any((p / "manifest.json").exists()
                       for p in fdir.iterdir() if p.is_dir())
    if not has_base:
        return CheckResult("fixtures", "SKIP",
                           "no baseline yet (make_testbed.py --capture-fixtures)")
    code, out = _run([sys.executable, str(ROOT / "scripts" / "make_testbed.py"),
                      "--verify-fixtures"], timeout=120,
                     extra_env={"PYTHONUTF8": "1"})
    return CheckResult("fixtures", "PASS" if code == 0 else "FAIL", out,
                       time.monotonic() - t0)


CHECKS = {
    "module-tests": check_module_tests,
    "undefined-names": check_undefined_names,
    "registry": check_registry,
    "fake-provider": check_fake_provider,
    "fixtures": check_fixtures,
    "frontend-js": check_frontend_js,
}


def main() -> int:
    # GBK 控制台容错：检查输出可能含任意字节，坏字符降级为 ?，不让报告本身崩掉
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    only = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--only" else None
    if only and only not in CHECKS:
        print(f"unknown check: {only}; choose from {list(CHECKS)}")
        return 2
    names = [only] if only else list(CHECKS)
    results: list[CheckResult] = []
    for name in names:
        print(f"-- {name} ...", flush=True)
        r = CHECKS[name]()
        results.append(r)
        print(f"   [{r.status}] {name} ({r.seconds:.1f}s)"
              + (f"\n{r.detail}" if r.detail else ""), flush=True)
    print("\n==== summary ====")
    for r in results:
        print(f"  [{r.status}] {r.name}")
    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
