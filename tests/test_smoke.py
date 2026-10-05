import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from aitobuild import __version__


def test_version_exists() -> None:
    assert isinstance(__version__, str)
    assert __version__


@pytest.mark.parametrize("command_exit_code", [0, 4])
def test_simulation_exit_status_matches_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_exit_code: int,
) -> None:
    simulation_dir = Path(__file__).resolve().parents[1] / "sim"
    spec = importlib.util.spec_from_file_location(
        "run_local_simulation", simulation_dir / "run_local_simulation.py"
    )
    assert spec is not None and spec.loader is not None
    simulation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(simulation)

    root = tmp_path / "sim"
    root.mkdir()
    shutil.copytree(simulation_dir / "repo-fixture", root / "repo-fixture")
    shutil.copytree(simulation_dir / "payloads", root / "payloads")
    payload_file = root / "payloads" / "developer-run.json"
    payload = json.loads(payload_file.read_text(encoding="utf-8"))
    test_path = "tests/test_math_ops.py" if command_exit_code == 0 else "tests/not_found.py"
    payload["commands"] = [f"python -m pytest -q {test_path}"]
    payload_file.write_text(json.dumps(payload), encoding="utf-8")

    for line in (simulation_dir / ".env.simulation").read_text(encoding="utf-8").splitlines():
        if line.startswith("AITOBUILD_"):
            name, value = line.split("=", 1)
            monkeypatch.setenv(name, value)
    monkeypatch.delenv("AITOBUILD_FOUNDRY_ENDPOINT", raising=False)
    monkeypatch.delenv("AITOBUILD_FOUNDRY_API_KEY", raising=False)
    monkeypatch.delenv("AITOBUILD_DEVELOPER_SESSION_CONTAINER_BIND_PATH", raising=False)
    output = tmp_path / "report.json"

    exit_code = simulation.run_simulation(
        root=root,
        env_file=simulation_dir / ".env.simulation",
        output_file=output,
        use_session=False,
        requested_session_id=None,
        live_model=False,
        agent_prompt="Read the fixture.",
        auto_approve_agent_tools=False,
        agent_max_approval_rounds=3,
        live_output=False,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == (0 if command_exit_code == 0 else 1)
    assert report["succeeded"] is (command_exit_code == 0)
    assert report["developer_run"]["accepted"] is (command_exit_code == 0)
    assert report["developer_run"]["command_outcomes"][0]["exit_code"] == command_exit_code
    assert report["webhook_initial"]["route"] == "developer.preview_required"
    assert report["webhook_replayed"]["route"] == "developer.async.webhook"
    assert report["generated_file_exists"]
