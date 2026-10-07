from __future__ import annotations

import asyncio
import builtins
import importlib
import json
from pathlib import Path

from agent_framework import Agent, InMemoryCheckpointStorage
from agent_framework.exceptions import WorkflowConvergenceException
from agent_framework.openai import OpenAIChatCompletionClient
import httpx
from openai import AsyncOpenAI
import pytest

from aitobuild.config import RuntimeConfig
from aitobuild.organization import FileDefinitionStore, parse_organization_definition
from aitobuild.organization_runtime import (
    ModelProfile, ToolProfile, WorkflowLimits, WorkflowOperation, bootstrap_organization,
    build_organization_workflows, create_model_profile,
)
from aitobuild.policy import ActionClass, AgentRole
from aitobuild.runtime import FrameworkBindings


EXAMPLE = Path(__file__).resolve().parents[1] / "config/organization.example.json"


def snapshot(tmp_path, document=None):
    content = json.dumps(document) if document is not None else EXAMPLE.read_text()
    return FileDefinitionStore(tmp_path / "definitions").save(parse_organization_definition(content))


class RecordingAgent:
    created = []

    def __init__(self, **kwargs):
        self.options = kwargs
        self.created.append(self)


def test_configured_instances_profiles_prompts_and_separate_state(tmp_path) -> None:
    document = json.loads(EXAMPLE.read_text())
    document["agents"][2]["model_profile"] = "secondary"
    primary, secondary = object(), object()
    runtime = bootstrap_organization(
        snapshot(tmp_path, document),
        model_profiles={"primary": ModelProfile("openai", primary), "secondary": ModelProfile("openai", secondary)},
        default_model_profile="primary", bindings=FrameworkBindings(agent_class=RecordingAgent),
        state_dir=tmp_path / "state",
    )
    first, second = runtime.agents["developer_one"], runtime.agents["developer_two"]
    assert first is not second
    assert first.options["client"] is primary and second.options["client"] is secondary
    assert first.options["name"] == "developer_one"
    assert "only from the provided isolation task" in first.options["instructions"]
    assert document["agents"][1]["instructions"] in first.options["instructions"]
    assert first.options["context_providers"][0] is not second.options["context_providers"][0]
    assert (tmp_path / "state" / "product" / runtime.snapshot.revision / "developer_one").exists()
    assert (tmp_path / "state" / "product" / runtime.snapshot.revision / "developer_two").exists()
    with pytest.raises(TypeError):
        runtime.agents["extra"] = object()


@pytest.mark.parametrize("case", ["default", "model", "tools", "role", "native"])
def test_profile_resolution_fails_before_creating_any_agent(tmp_path, case) -> None:
    document = json.loads(EXAMPLE.read_text())
    if case == "model":
        document["agents"][2]["model_profile"] = "missing"
    if case in {"tools", "role"}:
        document["agents"][2]["tool_profile"] = "pm_tools"
    RecordingAgent.created.clear()
    with pytest.raises((ValueError, PermissionError, RuntimeError)):
        bootstrap_organization(
            snapshot(tmp_path, document), model_profiles={"primary": ModelProfile("openai", object())},
            default_model_profile="missing" if case == "default" else "primary",
            tool_profiles={"pm_tools": ToolProfile(AgentRole.PM)} if case == "role" else {},
            bindings=FrameworkBindings() if case == "native" else FrameworkBindings(agent_class=RecordingAgent),
        )
    assert RecordingAgent.created == []


def test_native_developer_tools_remain_per_run(tmp_path) -> None:
    document = json.loads(EXAMPLE.read_text())
    document["agents"][0]["tool_profile"] = "pm_tools"
    document["agents"][1]["tool_profile"] = "dev_tools"
    pm_tool, dev_tool = lambda: None, lambda: None
    runtime = bootstrap_organization(
        snapshot(tmp_path, document), model_profiles={"primary": ModelProfile("openai", object())},
        default_model_profile="primary", bindings=FrameworkBindings(agent_class=RecordingAgent),
        tool_profiles={
            "pm_tools": ToolProfile(AgentRole.PM, (pm_tool,)),
            "dev_tools": ToolProfile(AgentRole.DEVELOPER, (dev_tool,), (ActionClass.REPO_WRITE,)),
        },
    )
    assert runtime.agents["planner"].options["tools"] == [pm_tool]
    assert runtime.agents["developer_one"].options["tools"] == []


def test_tool_profiles_cannot_extend_role_policy() -> None:
    with pytest.raises(PermissionError):
        ToolProfile(AgentRole.PM, (), (ActionClass.REPO_WRITE,))


def test_optional_tool_profile_roundtrip(tmp_path) -> None:
    original = snapshot(tmp_path)
    assert all(agent.tool_profile is None for agent in original.definition.agents)
    assert FileDefinitionStore(tmp_path / "definitions").get("product", original.revision) == original


def test_operator_model_profile_mock_and_missing_native_configuration() -> None:
    config = RuntimeConfig(None, None, "test-model", True)
    profile = create_model_profile(config, bindings=FrameworkBindings())
    assert profile.mode == "mock" and profile.client["model"] == "test-model"
    with pytest.raises(RuntimeError):
        create_model_profile(RuntimeConfig(None, None, "test-model", False), bindings=FrameworkBindings())


def operation_node(identifier, operation=None):
    return {"id": identifier, "kind": "operation", "operation": operation or identifier}


def graph(nodes=None, edges=None, outputs=None, **metadata):
    nodes = nodes or [operation_node("probe", "definition_probe")]
    return {
        "format": "python_graph", "start": nodes[0]["id"], "nodes": nodes,
        "edges": edges or [], "outputs": outputs or [nodes[-1]["id"]], **metadata,
    }


def configured_runtime(tmp_path, workflow_document):
    document = json.loads(EXAMPLE.read_text())
    document["workflows"][0]["document"] = workflow_document
    return bootstrap_organization(
        snapshot(tmp_path, document), model_profiles={"primary": ModelProfile("openai", object())},
        default_model_profile="primary", bindings=FrameworkBindings(agent_class=RecordingAgent),
    )


def test_native_conditional_loop_and_operator_operation_execution(tmp_path) -> None:
    calls = []

    async def increment(value):
        calls.append(value)
        return value + 1

    runtime = configured_runtime(tmp_path, graph(
        nodes=[operation_node("increment"), operation_node("result", "identity")],
        edges=[
            {"source": "increment", "target": "increment", "condition": "again"},
            {"source": "increment", "target": "result", "condition": "done"},
        ],
    ))
    workflows = build_organization_workflows(
        runtime, operations={"increment": WorkflowOperation(increment), "identity": WorkflowOperation(lambda message: message)},
        predicates={"again": lambda message: message < 3, "done": lambda message: message == 3},
    )
    assert calls == []
    result = asyncio.run(workflows["definition_probe"].run(0))
    assert result.get_outputs() == [3]
    assert calls == [0, 1, 2]


@pytest.mark.parametrize("message,expected", [(True, "first"), (False, "fallback")])
def test_native_switch_routes_without_declarative_or_dotnet(tmp_path, monkeypatch, message, expected) -> None:
    import aitobuild.organization_runtime as module
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"agent_framework_declarative", "powerfx", "clr", "pythonnet"}:
            raise AssertionError(f"Unexpected non-Python workflow dependency: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    module = importlib.reload(module)
    calls = []
    runtime = configured_runtime(tmp_path, graph(
        nodes=[operation_node("start", "identity"), operation_node("first"), operation_node("second"), operation_node("fallback")],
        edges=[{"kind": "switch", "source": "start", "cases": [
            {"condition": "selected", "target": "first"}, {"condition": "selected_again", "target": "second"},
        ], "default": "fallback"}], outputs=["first", "second", "fallback"],
    ))

    def selected(value):
        calls.append(value)
        return value

    workflow = module.build_organization_workflows(
        runtime, operations={name: WorkflowOperation(lambda value, name=name: name) for name in ("first", "second", "fallback")}
        | {"identity": WorkflowOperation(lambda value: value)},
        predicates={"selected": selected, "selected_again": selected},
    )["definition_probe"]
    assert calls == []
    assert asyncio.run(workflow.run(message)).get_outputs() == [expected]
    assert calls == ([True] if message else [False, False])


def test_native_fan_out_fan_in_and_checkpoint_storage(tmp_path) -> None:
    runtime = configured_runtime(tmp_path, graph(
        nodes=[operation_node("start", "identity"), operation_node("left"), operation_node("right"), operation_node("join")],
        edges=[{"kind": "fan_out", "source": "start", "targets": ["left", "right"]},
               {"kind": "fan_in", "sources": ["left", "right"], "target": "join"}],
    ))
    calls = []

    def join(messages):
        calls.append(messages)
        return sorted(messages)

    storage = InMemoryCheckpointStorage()
    workflow = build_organization_workflows(runtime, checkpoint_storage=storage, operations={
        "identity": WorkflowOperation(lambda message: message),
        "left": WorkflowOperation(lambda message: "left:" + message),
        "right": WorkflowOperation(lambda message: "right:" + message),
        "join": WorkflowOperation(join),
    })["definition_probe"]

    async def check():
        result = await workflow.run("probe")
        assert result.get_outputs() == [["left:probe", "right:probe"]]
        checkpoints = await storage.list_checkpoints(
            workflow_name=f"product.{runtime.snapshot.revision}.definition_probe",
        )
        assert checkpoints

    asyncio.run(check())
    assert len(calls) == 1


@pytest.mark.parametrize("case", ["integer", "coroutine"])
def test_native_predicates_reject_non_boolean_results(tmp_path, case) -> None:
    async def async_value():
        return True

    workflow = build_organization_workflows(
        configured_runtime(tmp_path, graph(
            [operation_node("start", "identity"), operation_node("result", "identity")],
            [{"source": "start", "target": "result", "condition": "selected"}],
        )), operations={"identity": WorkflowOperation(lambda message: message)},
        predicates={"selected": lambda message: 1 if case == "integer" else async_value()},
    )["definition_probe"]
    with pytest.raises(ValueError, match="must return a boolean"):
        asyncio.run(workflow.run("probe"))


def test_native_runner_iteration_limit_stops_feedback_loop(tmp_path) -> None:
    calls = []
    workflow = build_organization_workflows(
        configured_runtime(tmp_path, graph(
            [operation_node("loop", "identity")],
            [{"source": "loop", "target": "loop"}], max_iterations=2,
        )), operations={"identity": WorkflowOperation(lambda message: calls.append(message) or message)},
    )["definition_probe"]
    with pytest.raises(WorkflowConvergenceException, match="iterations"):
        asyncio.run(workflow.run("probe"))
    assert calls == ["probe", "probe"]


def test_operation_payload_is_literal_data_not_an_expression(tmp_path) -> None:
    workflow = build_organization_workflows(
        configured_runtime(tmp_path, graph()),
        operations={"definition_probe": WorkflowOperation(lambda message: message)},
    )["definition_probe"]
    assert asyncio.run(workflow.run("=Local.value")).get_outputs() == ["=Local.value"]


@pytest.mark.parametrize("case", [
    "unknown_kind", "unknown_field", "inline_agents", "file_agents", "trigger", "dynamic_agent",
    "unknown_agent", "dynamic_operation", "unknown_operation", "http", "mcp", "duplicate_id",
    "missing_start", "unreachable_node", "unknown_target", "boolean_iterations", "oversized_iterations",
    "write_binding", "unknown_predicate", "expression", "unknown_output", "duplicate_output",
    "duplicate_connection", "invalid_edge_kind", "mixed_edge_fields", "duplicate_targets", "unknown_format",
    "async_predicate", "noncallable_predicate", "noncallable_operation",
])
def test_workflow_admission_rejects_unsafe_or_invalid_graphs(tmp_path, case) -> None:
    document = graph([operation_node("first", "operation"), operation_node("second", "operation")],
                     [{"source": "first", "target": "second"}])
    predicates = {}
    if case == "unknown_kind":
        document["nodes"][0]["kind"] = "UnrecognizedTypo"
    elif case == "unknown_field":
        document["nodes"][0]["execute"] = "untrusted command"
    elif case in {"inline_agents", "file_agents", "http", "mcp"}:
        document["nodes"][0] = {"id": "first", "kind": "agent", "agent": {"name": "planner", "file": "../agent.yaml"}} if "agents" in case else {"id": "first", "kind": case, "url": "https://untrusted.example"}
    elif case == "trigger":
        document["trigger"] = {}
    elif case in {"dynamic_agent", "unknown_agent"}:
        document["nodes"][0] = {"id": "first", "kind": "agent", "agent": "=Local.agent" if case == "dynamic_agent" else "outsider"}
    elif case in {"dynamic_operation", "unknown_operation"}:
        document["nodes"][0]["operation"] = "=Local.function" if case == "dynamic_operation" else "outsider"
    elif case == "duplicate_id":
        document["nodes"][1]["id"] = "first"
    elif case == "missing_start":
        document["start"] = "missing"
    elif case == "unreachable_node":
        document["edges"] = []
    elif case == "unknown_target":
        document["edges"][0]["target"] = "missing"
    elif case in {"boolean_iterations", "oversized_iterations"}:
        document["max_iterations"] = True if case == "boolean_iterations" else 101
    elif case in {"unknown_predicate", "expression"}:
        document["edges"][0]["condition"] = "missing" if case == "unknown_predicate" else "=Local.ready"
    elif case == "unknown_output":
        document["outputs"] = ["missing"]
    elif case == "duplicate_output":
        document["outputs"] *= 2
    elif case == "duplicate_connection":
        document["edges"] *= 2
    elif case == "invalid_edge_kind":
        document["edges"][0]["kind"] = "goto"
    elif case == "mixed_edge_fields":
        document["edges"][0]["targets"] = ["second"]
    elif case == "duplicate_targets":
        document["edges"] = [{"kind": "fan_out", "source": "first", "targets": ["second", "second"]}]
    elif case == "unknown_format":
        document["format"] = "eval"
    elif case in {"async_predicate", "noncallable_predicate"}:
        async def selected(message):
            return True
        predicates["selected"] = selected if case == "async_predicate" else True
    calls = []
    operations = {"operation": WorkflowOperation(None if case == "noncallable_operation" else lambda message: calls.append(True), ActionClass.REPO_WRITE if case == "write_binding" else ActionClass.READ_ONLY)}
    with pytest.raises((ValueError, PermissionError)):
        build_organization_workflows(configured_runtime(tmp_path, document), operations=operations, predicates=predicates)
    assert calls == []


@pytest.mark.parametrize("limit", ["nodes", "edges", "size", "iterations"])
def test_workflow_operator_admission_limits(tmp_path, limit) -> None:
    document = graph([operation_node("start", "identity"), operation_node("middle", "identity"), operation_node("end", "identity")],
                     [{"source": "start", "target": "middle"}, {"source": "middle", "target": "end"}])
    limits = WorkflowLimits(**{f"max_{limit}": 1} if limit in {"nodes", "edges", "iterations"} else {"max_document_bytes": 1})
    with pytest.raises(ValueError):
        build_organization_workflows(configured_runtime(tmp_path, document), limits=limits,
                                     operations={"identity": WorkflowOperation(lambda message: message)})


def test_mock_agent_invocation_is_not_native_execution(tmp_path) -> None:
    document = json.loads(EXAMPLE.read_text())
    document["workflows"][0]["document"] = graph([{"id": "planner", "kind": "agent", "agent": "planner"}])
    runtime = bootstrap_organization(
        snapshot(tmp_path, document), model_profiles={"primary": ModelProfile("mock", {})},
        default_model_profile="primary", bindings=FrameworkBindings(),
    )
    with pytest.raises(ValueError, match="mock"):
        build_organization_workflows(runtime)


def test_native_agent_reference_cannot_escape_routed_team(tmp_path) -> None:
    document = json.loads(EXAMPLE.read_text())
    document["teams"][0]["members"].remove("developer_two")
    document["routes"][0]["delegation"]["eligible_agents"] = ["developer_one"]
    document["workflows"][0]["document"] = graph([{"id": "developer", "kind": "agent", "agent": "developer_two"}])
    runtime = bootstrap_organization(
        snapshot(tmp_path, document), model_profiles={"primary": ModelProfile("openai", object())},
        default_model_profile="primary", bindings=FrameworkBindings(agent_class=RecordingAgent),
    )
    with pytest.raises(PermissionError, match="routed team"):
        build_organization_workflows(runtime)


def test_actual_native_agents_use_configured_clients_without_live_requests(tmp_path) -> None:
    requests = []

    def model_reply(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={
            "id": f"response-{len(requests)}", "object": "chat.completion", "created": 1,
            "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "RESULT:" + body["model"]}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        })

    document = json.loads(EXAMPLE.read_text())
    document["agents"][1]["model_profile"] = "implementation"
    document["workflows"][0]["document"] = graph(
        [{"id": "plan", "kind": "agent", "agent": "planner"},
         {"id": "implement", "kind": "agent", "agent": "developer_one"}],
        [{"source": "plan", "target": "implement"}], outputs=["plan", "implement"],
    )

    async def check() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(model_reply)) as http_client:
            async with AsyncOpenAI(api_key="test-key", http_client=http_client) as client:
                runtime = bootstrap_organization(
                    snapshot(tmp_path, document),
                    model_profiles={
                        "planning": ModelProfile("openai", OpenAIChatCompletionClient(model="plan-model", async_client=client)),
                        "implementation": ModelProfile("openai", OpenAIChatCompletionClient(model="dev-model", async_client=client)),
                    },
                    default_model_profile="planning", bindings=FrameworkBindings(agent_class=Agent),
                )
                workflow = build_organization_workflows(runtime)["definition_probe"]
                result = await workflow.run("Inspect the approved assignment, without edits.")
                assert [output.text for output in result.get_outputs()] == [
                    "RESULT:plan-model", "RESULT:dev-model",
                ]

    asyncio.run(check())
    assert [request["model"] for request in requests] == ["plan-model", "dev-model"]
    assert "PM agent" in str(requests[0]["messages"])
    assert "Developer agent" in str(requests[1]["messages"])
    assert "RESULT:plan-model" in str(requests[1]["messages"])