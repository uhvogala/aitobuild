"""Agent-driven Developer tool evaluation over disposable simulation fixtures."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
from time import perf_counter
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from aitobuild.app import create_app
from aitobuild.config import load_config
from sim.run_local_simulation import (
    _rebase_session_bind_path_for_sandbox,
    assert_live_model_config,
    call_checked,
    copied_fixture_repo,
    internal_headers,
    load_env_file,
)

MEMORY_TOOLS = {
    "file_memory_write", "file_memory_read", "file_memory_delete", "file_memory_ls",
    "file_memory_grep", "file_memory_replace", "file_memory_replace_lines",
}
BROWSER_TOOLS = {
    "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_click",
    "browser_type", "browser_press_key", "browser_select_option", "browser_tabs",
    "browser_close", "browser_take_screenshot", "browser_resize", "browser_wait_for",
    "browser_console_messages", "browser_network_requests",
}
DEVELOPER_TOOLS = {
    "developer_start_session", "developer_stop_session", "developer_run_command",
    "developer_read_file", "developer_write_file", "developer_edit_file",
    "developer_shell", "developer_processes",
    "developer_read_output",
    "developer_find_files", "developer_search_files",
}


def grade_case(
    *, name: str, response: dict[str, Any], required_tools: set[str],
    checks: dict[str, bool], elapsed_seconds: float,
    expected_errors: int = 0,
) -> dict[str, Any]:
    trace = response.get("tool_trace", [])
    executed = {entry["name"] for entry in trace}
    successful = {entry["name"] for entry in trace if entry.get("ok") is True}
    missing = sorted(required_tools - successful)
    counts = Counter(entry["name"] for entry in trace)
    tool_errors = sum(entry.get("ok") is not True for entry in trace)
    return {
        "name": name,
        "passed": bool(response.get("completed") and not missing and all(checks.values())
                   and tool_errors == expected_errors),
        "checks": checks,
        "required_tools": sorted(required_tools),
        "executed_tools": sorted(executed),
        "missing_successful_tools": missing,
        "tool_calls": len(trace),
        "tool_errors": tool_errors,
        "expected_tool_errors": expected_errors,
        "unexpected_tool_errors": max(0, tool_errors - expected_errors),
        "repeated_tool_calls": sum(max(0, count - 1) for count in counts.values()),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "usage": response.get("usage", {}),
        "approval_rounds": response.get("approval_rounds", 0),
        "completed": response.get("completed", False),
        "error": response.get("error"),
        "output_text": response.get("output_text", "")[:4000],
        "trace": trace,
    }


def summarize(cases: list[dict[str, Any]], expected_tools: set[str]) -> dict[str, Any]:
    successful = {
        entry["name"] for case in cases for entry in case["trace"] if entry.get("ok") is True
    }
    usage: Counter[str] = Counter()
    for case in cases:
        usage.update(case["usage"])
    missing = sorted(expected_tools - successful)
    return {
        "succeeded": bool(cases and all(case["passed"] for case in cases) and not missing),
        "cases_passed": sum(case["passed"] for case in cases),
        "cases_total": len(cases),
        "tool_coverage": round(len(expected_tools & successful) / len(expected_tools), 3)
        if expected_tools else 1.0,
        "expected_tools": sorted(expected_tools),
        "missing_successful_tools": missing,
        "tool_calls": sum(case["tool_calls"] for case in cases),
        "tool_errors": sum(case["tool_errors"] for case in cases),
        "unexpected_tool_errors": sum(case["unexpected_tool_errors"] for case in cases),
        "elapsed_seconds": round(sum(case["elapsed_seconds"] for case in cases), 3),
        "usage": dict(usage),
    }


def fixture_files(repo: Path) -> None:
    (repo / "src/demo_app/eval-large-output.txt").write_text(
        "LARGE_OUTPUT_BEGIN\n" + "x" * 50000 + "\nLARGE_OUTPUT_END\n", encoding="utf-8",
    )
    (repo / "tests/test_eval_contract.py").write_text(
        "from pathlib import Path\nimport sys\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n\n"
        "from demo_app.math_ops import add, subtract\n\n"
        "def test_eval_contract():\n"
        "    assert add(2, 3) == 5\n"
        "    assert subtract(7, 3) == 4\n", encoding="utf-8",
    )
    (repo / "src/demo_app/eval_worker.py").write_text(
        "from pathlib import Path\nimport signal\nimport sys\n\n"
        "if sys.argv[-1] == 'hold':\n"
        "    print('HOLD_READY', flush=True)\n"
        "    signal.pause()\n"
        "else:\n"
        "    value = input('VALUE: ')\n"
        "    Path('tests/terminal-result.txt').write_text(value)\n"
        "    print('INPUT_ACCEPTED', flush=True)\n", encoding="utf-8",
    )
    (repo / "tests/eval_browser_server.py").write_text(
        '''from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HTML = """<!doctype html><title>Tool Evaluation</title>
<label>Name <input id="name"></label>
<label>Mode <select id="mode"><option>basic</option><option>advanced</option></select></label>
<button onclick="submit()">Submit</button><p id="status">Ready</p>
<a href="/second">Second page</a>
<script>
async function submit() {
  const name = document.querySelector('#name').value;
  const mode = document.querySelector('#mode').value;
  await fetch('/submit?name=' + encodeURIComponent(name) + '&mode=' + mode);
  document.querySelector('#status').textContent = 'Submitted ' + name + ' ' + mode;
  console.log('EVAL_SUBMITTED');
}
</script>"""

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = urlparse(self.path)
        if route.path == '/submit':
            Path('tests/browser-result.json').write_text(json.dumps(parse_qs(route.query)))
            content = b'OK'
        elif route.path == '/second':
            content = b'<title>Second page</title><h1>Second page</h1>'
        else:
            content = HTML.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(content)

HTTPServer(('127.0.0.1', 8765), Handler).serve_forever()
''', encoding="utf-8",
    )


def run_evaluation(*, env_file: Path, output: Path, browser: bool = True,
                   recovery: bool = False, model: str | None = None,
                   invoke_timeout: int | None = None) -> int:
    load_env_file(env_file)
    base = load_config()
    config = replace(base, runtime=replace(base.runtime, allow_mock_model=False,
                                          foundry_model=model or base.runtime.foundry_model), developer=replace(
        base.developer, execution_mode="container_session", enable_browser=browser,
        enable_mcp_adapters=False,
        agent_invoke_timeout_seconds=invoke_timeout or base.developer.agent_invoke_timeout_seconds,
        state_dir=".aitobuild/evaluation",
    ))
    assert_live_model_config(config)
    root = Path(__file__).resolve().parent
    output = output.resolve()
    cases: list[dict[str, Any]] = []
    session_id = "eval-" + uuid4().hex[:12]
    sentinel = "memory-" + uuid4().hex[:12]
    expected_tools = DEVELOPER_TOOLS | MEMORY_TOOLS | (BROWSER_TOOLS if browser else set())
    report: dict[str, Any] = {
        "schema_version": 1, "started_at": datetime.now(UTC).isoformat(),
        "model": config.runtime.foundry_model, "endpoint": config.runtime.foundry_endpoint,
        "session_id": session_id, "browser_enabled": browser,
        "recovery_mode": recovery,
        "scope": "Developer tools, native file memory, and optional browser allowlist",
        "auto_approvals": "Enabled only for this disposable fixture; not a production policy",
        "cases": cases,
    }

    def save() -> None:
        report.update(summarize(cases, expected_tools))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    with copied_fixture_repo(root / "repo-fixture") as (run_root, repo):
        fixture_files(repo)
        _rebase_session_bind_path_for_sandbox(workspace_root=root.parent, sandbox_repo=repo)
        config = replace(config, developer=replace(config.developer, session_container_bind_path=(
            load_config().developer.session_container_bind_path
        )))
        report.update(simulation_root=str(run_root), sandbox_repo=str(repo))
        headers = internal_headers(config)
        client = TestClient(create_app(config))
        client.__enter__()
        started = call_checked(client, "POST", "/internal/developer/session/start",
                               headers=headers, json={"session_id": session_id})
        workspace = Path(started["workspace"])
        container = started["container_name"]
        report["developer_workspace"] = str(workspace)

        def invoke(prompt: str, *, identity: str = session_id,
               browser_tools: bool = False) -> tuple[dict[str, Any], float]:
            begin = perf_counter()
            response = client.post("/internal/developer/agent/run", headers=headers, json={
                "input": prompt + "\nExecute the tools; do not merely describe a plan. "
                "Do not touch any other Developer or public service. Do not stop your session unless asked.",
                "session_id": identity, "auto_approve_tools": True, "max_approval_rounds": 20,
                "include_tool_trace": True,
                "use_browser": browser_tools,
            })
            body = response.json()
            if response.status_code >= 400:
                body = {"completed": False, "error": body, "tool_trace": []}
            return body, perf_counter() - begin

        def record(name: str, response: dict[str, Any], elapsed: float,
               required: set[str], checks: dict[str, bool], expected_errors: int = 0) -> None:
            case = grade_case(name=name, response=response, required_tools=required,
                      checks=checks, elapsed_seconds=elapsed, expected_errors=expected_errors)
            cases.append(case)
            print(f"[eval] {name}: {'PASS' if case['passed'] else 'FAIL'}; "
                  f"calls={case['tool_calls']} errors={case['tool_errors']} "
                  f"missing={case['missing_successful_tools']}", flush=True)
            save()

        def action_seen(response: dict[str, Any], tool: str, action: str) -> bool:
            return any(entry["name"] == tool and entry.get("ok") is True
                       and entry["arguments"].get("action") == action
                       for entry in response.get("tool_trace", []))

        try:
            response, elapsed = invoke(
                "Start/reuse your bound Developer session with developer_start_session. "
                "Use developer_find_files with glob **/math_ops.py to find the source file. "
                "Use developer_search_files for literal def add, with globs **/math_ops.py, to locate its definition. "
                "Read src/demo_app/math_ops.py using developer_read_file. Use developer_write_file "
                "to create src/demo_app/eval_note.txt containing exactly tool-eval-ok followed by a newline. "
                     + ("Exercise edit recovery: make exactly one developer_edit_file attempt with "
                         "old_text EVAL_STALE_CONTEXT; follow its error instructions and re-read. "
                         if recovery else "")
                     + "Use developer_edit_file to add subtract(left, right) returning left-right "
                "to math_ops.py without changing add. Use developer_run_command to run "
                "python -m pytest -q -p no:cacheprovider. Check the exit code."
            )
            verification = subprocess.run(
                ["docker", "exec", container, "python", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                capture_output=True, text=True, timeout=60, check=False,
            )
            report["pytest_verifier"] = {"exit_code": verification.returncode,
                                         "output": (verification.stdout + verification.stderr)[-4000:]}
            edit_calls = [item for item in response.get("tool_trace", [])
                          if item["name"] == "developer_edit_file"]
            record("files_and_edit_recovery" if recovery else "files", response, elapsed, {
                "developer_start_session", "developer_read_file", "developer_write_file",
                "developer_edit_file", "developer_run_command", "developer_find_files", "developer_search_files",
            }, {
                "file_discovered": any(
                    item["name"] == "developer_find_files" and item.get("ok")
                    and any(match["path"] == "src/demo_app/math_ops.py"
                            for match in item.get("result", {}).get("results", []))
                    for item in response.get("tool_trace", [])
                ),
                "definition_found": any(
                    item["name"] == "developer_search_files" and item.get("ok")
                    and any(match["path"] == "src/demo_app/math_ops.py" and match["line_number"] == 4
                            for match in item.get("result", {}).get("results", []))
                    for item in response.get("tool_trace", [])
                ),
                "pytest_passes": verification.returncode == 0,
                "written_file_matches": (workspace / "src/demo_app/eval_note.txt").exists()
                and (workspace / "src/demo_app/eval_note.txt").read_text() == "tool-eval-ok\n",
                "edit_applied": bool(edit_calls and edit_calls[-1]["ok"]),
                **({"edit_error_then_recovery": any(not item["ok"] for item in edit_calls)} if recovery else {}),
            }, expected_errors=1 if recovery else 0)

            response, elapsed = invoke(
                "Exercise developer_shell and developer_processes. Start a detached shell command "
                "python -u src/demo_app/eval_worker.py input with wait_seconds=0. Save its shell_id "
                "and next_cursor; list shells, read that same ID, then send exactly terminal-ok as "
                "one input with Enter, and read its retained result using a cursor. "
                "Start python -u src/demo_app/eval_worker.py hold detached. List processes filtering "
                "eval_worker.py, inspect the actual Python worker's process_handle (not its parent sh), "
                "send TERM to that worker, and read the same shell until exit is verified. "
                "Start another hold worker and stop its whole shell using developer_shell stop. "
                "Finally start command sh -c 'echo EXPECTED_FAILURE; exit 7', and read its retained "
                "exit code. The exit 7 is expected, not a reason to rerun. Never guess handles."
            )
            shell_results = [item.get("result", {}) for item in response.get("tool_trace", [])
                             if item["name"] == "developer_shell" and isinstance(item.get("result"), dict)]
            record("terminals_and_processes", response, elapsed,
                   {"developer_shell", "developer_processes"}, {
                       **{f"shell_{action}": action_seen(response, "developer_shell", action)
                          for action in ("start", "read", "send", "list", "stop")},
                       **{f"process_{action}": action_seen(response, "developer_processes", action)
                          for action in ("list", "inspect", "signal")},
                       "interactive_result_matches": (workspace / "tests/terminal-result.txt").exists()
                       and (workspace / "tests/terminal-result.txt").read_text() == "terminal-ok",
                       "detached_running_handle": any(item.get("status") == "running" for item in shell_results),
                       "exit_7_retained": any(item.get("exit_code") == 7 for item in shell_results),
                       "stopped_exit_retained": any(item.get("exit_code") == 137 for item in shell_results),
                       "term_exit_verified": any(item.get("exit_code") == 143 for item in shell_results),
                   })

            response, elapsed = invoke(
                f"Exercise all seven native file_memory tools. Write eval-memory.md containing exactly "
                f"{sentinel}\nstatus=draft\n. List memory files and read this file. Use grep to find "
                "status=draft. Use replace to change status=draft to status=reviewed, then "
                "replace_lines to change line 2 to status=verified followed by a newline. "
                "Write eval-temp.md, then delete eval-temp.md. Finally read eval-memory.md and "
                "leave it intact for a restart verification."
            )
            memory_files = list((repo / config.developer.state_dir / "memory").rglob("eval-memory.md"))
            record("native_memory", response, elapsed, MEMORY_TOOLS, {
                "persistent_file_matches": any(path.read_text() == f"{sentinel}\nstatus=verified\n"
                                               for path in memory_files),
                "temporary_memory_deleted": not list(
                    (repo / config.developer.state_dir / "memory").rglob("eval-temp.md")
                ),
            })

            client.__exit__(None, None, None)
            client = TestClient(create_app(config))
            client.__enter__()
            response, elapsed = invoke(
                "The API application restarted. Use file_memory_read on eval-memory.md and "
                "developer_read_file on src/demo_app/eval_note.txt to verify durable state."
            )
            record("restart_recovery", response, elapsed, {"file_memory_read", "developer_read_file"}, {
                "memory_read_contains_saved_sentinel": any(
                    item["name"] == "file_memory_read" and sentinel in str(item.get("result", ""))
                    for item in response.get("tool_trace", [])
                ),
            })

            response, elapsed = invoke(
                "Read src/demo_app/eval-large-output.txt using developer_read_file. Its output is "
                "intentionally large. Inspect the saved-output preview and use developer_read_output "
                "to read only the first 256 bytes using its actual output_id. Do not fetch every page."
            )
            output_calls = response.get("tool_trace", [])
            saved = [item.get("result", {}) for item in output_calls
                     if isinstance(item.get("result"), dict) and item["result"].get("output_saved")]
            record("large_output_guard", response, elapsed, {"developer_read_file", "developer_read_output"}, {
                "result_spilled": bool(saved),
                "complete_output_retained": any(
                    (repo / config.developer.state_dir / "outputs" / session_id / f"{item['output_id']}.txt").read_text()
                    == (workspace / "src/demo_app/eval-large-output.txt").read_text()
                    for item in saved
                ),
                "paged_read_is_bounded": any(item["name"] == "developer_read_output"
                                            and isinstance(item.get("result"), dict)
                                            and len(item["result"].get("text", "")) <= 256
                                            and item["result"].get("has_more") is True
                                            for item in output_calls),
            })

            if browser:
                response, elapsed = invoke(
                    "Use developer_shell to start python -u tests/eval_browser_server.py detached; "
                    "then use EVERY available browser tool against http://127.0.0.1:8765 only. "
                    "Navigate and snapshot; resize to 1024x768. Use browser_type on Name to enter Agent, "
                    "browser_press_key to press Tab, browser_select_option to choose advanced, and "
                    "browser_click to Submit. Wait for text Submitted Agent advanced. Take a screenshot. "
                    "Inspect console messages and network requests. Click Second page, then use "
                    "browser_navigate_back. Use browser_tabs to list, create and close a spare tab. "
                    "Call browser_close LAST. Use snapshot refs; do not invent them. "
                    "Finally stop the server's shell, retaining its logs.",
                    browser_tools=True,
                )
                result_file = workspace / "tests/browser-result.json"
                record("browser", response, elapsed, BROWSER_TOOLS | {"developer_shell"}, {
                    "submitted_form_matches": result_file.exists()
                    and json.loads(result_file.read_text()) == {"name": ["Agent"], "mode": ["advanced"]},
                    "screenshot_returned": any(
                        item["name"] == "browser_take_screenshot" and item.get("ok") is True
                        and "screenshot" in str(item.get("result", "")).lower()
                        for item in response.get("tool_trace", [])
                    ),
                })

            other_id = session_id + "-other"
            other = call_checked(client, "POST", "/internal/developer/session/start",
                                 headers=headers, json={"session_id": other_id})
            response, elapsed = invoke(
                "Use file_memory_ls and developer_read_file on src/demo_app/math_ops.py. "
                "This is a different Developer: list what you actually find without creating memory.",
                identity=other_id,
            )
            other_workspace = Path(other["workspace"])
            record("developer_isolation", response, elapsed, {"file_memory_ls", "developer_read_file"}, {
                "private_checkout": not (other_workspace / "src/demo_app/eval_note.txt").exists(),
                "seed_unchanged": "def subtract" not in (other_workspace / "src/demo_app/math_ops.py").read_text(),
                "private_memory": any(item["name"] == "file_memory_ls" and "eval-memory.md"
                                      not in str(item.get("result", ""))
                                      for item in response.get("tool_trace", [])),
            })

            response, elapsed = invoke(
                f"All verification is finished. Use developer_stop_session for {session_id} "
                "as your final tool call, then report whether it closed. Do not restart it."
            )
            record("session_shutdown", response, elapsed, {"developer_stop_session"}, {
                "session_closed": any(item["name"] == "developer_stop_session"
                                      and isinstance(item.get("result"), dict)
                                      and item["result"].get("closed") is True
                                      for item in response.get("tool_trace", [])),
            })
        except Exception as error:
            report["infrastructure_error"] = str(error)
        finally:
            try:
                report["cleanup"] = call_checked(
                    client, "POST", "/internal/developer/session/stop-all", headers=headers,
                )
            except Exception as error:
                report["cleanup"] = {"failed_count": 1, "error": str(error)}
            client.__exit__(None, None, None)
            save()
            report["succeeded"] = bool(report["succeeded"] and not report.get("infrastructure_error")
                                       and report["cleanup"].get("failed_count") == 0)
            output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[eval] passed={report['cases_passed']}/{report['cases_total']} "
          f"coverage={report['tool_coverage']:.0%}; report={output}", flush=True)
    return 0 if report["succeeded"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path("sim/.env.simulation.live.example"))
    parser.add_argument("--output", type=Path, default=Path("sim/.run-artifacts/tool-evaluation.json"))
    parser.add_argument("--no-browser", action="store_true", help="Exclude browser tools explicitly.")
    parser.add_argument("--recovery", action="store_true", help="Inject one expected stale-text edit failure.")
    parser.add_argument("--model", help="Override the deployment name without editing the env file.")
    parser.add_argument("--invoke-timeout", type=int, help="Per-framework-invocation deadline in seconds; defaults to env configuration.")
    args = parser.parse_args()
    if args.invoke_timeout is not None and args.invoke_timeout < 1:
        parser.error("--invoke-timeout must be >=1")
    return run_evaluation(env_file=args.env_file, output=args.output,
                          browser=not args.no_browser, recovery=args.recovery, model=args.model,
                          invoke_timeout=args.invoke_timeout)


if __name__ == "__main__":
    raise SystemExit(main())