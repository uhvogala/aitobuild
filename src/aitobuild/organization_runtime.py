"""Operator-owned profile resolution and configured native runtime assembly."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from inspect import isawaitable, iscoroutine, iscoroutinefunction
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from agent_framework import (
    AgentExecutor, Case, CheckpointStorage, Default, Executor, Workflow,
    WorkflowBuilder, WorkflowContext, handler,
)
from pydantic import TypeAdapter

from aitobuild.agents import AgentSpec, default_agent_specs
from aitobuild.config import RuntimeConfig
from aitobuild.organization import DefinitionSnapshot, Identifier, WorkflowGraphDefinition
from aitobuild.policy import ActionClass, AgentRole, assert_role_action_allowed
from aitobuild.runtime import (
    FrameworkBindings, _construct_foundry_client, _construct_openai_client,
    build_agent_handle, detect_framework_bindings,
)


@dataclass(frozen=True, slots=True)
class ModelProfile:
    mode: Literal["mock", "openai", "foundry"]
    client: Any


@dataclass(frozen=True, slots=True)
class ToolProfile:
    role: AgentRole
    tools: tuple[Callable[..., Any], ...] = ()
    actions: tuple[ActionClass, ...] = (ActionClass.READ_ONLY,)

    def __post_init__(self) -> None:
        for action in self.actions:
            assert_role_action_allowed(self.role, action)


@dataclass(frozen=True, slots=True)
class OrganizationRuntime:
    snapshot: DefinitionSnapshot
    agents: Mapping[str, Any]
    model_profiles: Mapping[str, str]
    tool_profiles: Mapping[str, str | None]
    modes: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class WorkflowOperation:
    handler: Callable[..., Any]
    action: ActionClass = ActionClass.READ_ONLY


@dataclass(frozen=True, slots=True)
class WorkflowLimits:
    max_iterations: int = 100
    max_nodes: int = 256
    max_edges: int = 512
    max_document_bytes: int = 131072

    def __post_init__(self) -> None:
        for value in (self.max_iterations, self.max_nodes, self.max_edges, self.max_document_bytes):
            if type(value) is not int or value <= 0:
                raise ValueError("Workflow admission limits must be positive integers")


WorkflowPredicate = Callable[[Any], bool]


class _OperationExecutor(Executor):
    def __init__(self, identifier: str, operation: WorkflowOperation) -> None:
        self._operation = operation
        super().__init__(id=identifier)

    @handler
    async def run_operation(self, message: Any, ctx: WorkflowContext[Any, Any]) -> None:
        result = self._operation.handler(message)
        if isawaitable(result):
            result = await result
        await ctx.send_message(result)
        await ctx.yield_output(result)


def _condition(name: str, predicate: WorkflowPredicate) -> WorkflowPredicate:
    def evaluate(message: Any) -> bool:
        result = predicate(message)
        if type(result) is not bool:
            if iscoroutine(result):
                result.close()
            raise ValueError(f"Workflow predicate {name} must return a boolean")
        return result

    evaluate.__name__ = name
    return evaluate


def _admit_graph(
    document: dict[str, Any], *, runtime: OrganizationRuntime,
    operations: Mapping[str, WorkflowOperation], predicates: Mapping[str, WorkflowPredicate],
    limits: WorkflowLimits,
) -> WorkflowGraphDefinition:
    if len(json.dumps(document).encode("utf-8")) > limits.max_document_bytes:
        raise ValueError("Native workflow document exceeds operator size limit")
    graph = WorkflowGraphDefinition.model_validate(document)
    if graph.max_iterations > limits.max_iterations:
        raise ValueError("Native workflow exceeds operator iteration limit")
    if len(graph.nodes) > limits.max_nodes:
        raise ValueError("Native workflow exceeds operator node limit")
    if sum(len(edge.connections()) for edge in graph.edges) > limits.max_edges:
        raise ValueError("Native workflow exceeds operator edge limit")
    for node in graph.nodes:
        if node.kind == "agent":
            if node.agent not in runtime.agents:
                raise ValueError(f"Unknown configured agent reference: {node.agent}")
            if runtime.modes[str(node.agent)] == "mock":
                raise ValueError("Native agent nodes cannot bind mock model handles")
        else:
            if node.operation not in operations:
                raise ValueError(f"Unknown operator workflow operation: {node.operation}")
            if operations[str(node.operation)].action is not ActionClass.READ_ONLY:
                raise PermissionError("Write/review workflow bindings require managed approved-task execution")
    for edge in graph.edges:
        conditions = [case.condition for case in edge.cases]
        if edge.condition is not None:
            conditions.append(edge.condition)
        if any(condition not in predicates for condition in conditions):
            raise ValueError("Workflow conditions must reference registered Python predicates")
    return graph


def build_organization_workflows(
    runtime: OrganizationRuntime, *,
    operations: Mapping[str, WorkflowOperation] | None = None,
    predicates: Mapping[str, WorkflowPredicate] | None = None,
    checkpoint_storage: CheckpointStorage | None = None,
    limits: WorkflowLimits = WorkflowLimits(),
) -> Mapping[str, Workflow]:
    registered = dict(operations or {})
    conditions = dict(predicates or {})
    for name, operation in registered.items():
        TypeAdapter(Identifier).validate_python(name)
        if not callable(operation.handler):
            raise ValueError("Operator workflow bindings must be callable")
    for name, predicate in conditions.items():
        TypeAdapter(Identifier).validate_python(name)
        if (not callable(predicate) or iscoroutinefunction(predicate)
                or iscoroutinefunction(getattr(predicate, "__call__", None))):
            raise ValueError("Workflow predicates must be synchronous Python callables")
    definition = runtime.snapshot.definition
    admitted = {
        workflow.id: _admit_graph(
            workflow.document, runtime=runtime, operations=registered, predicates=conditions, limits=limits,
        )
        for workflow in definition.workflows
    }
    teams = {team.id: set(team.members) for team in definition.teams}
    for route in definition.routes:
        agents = {node.agent for node in admitted[route.workflow].nodes if node.kind == "agent"}
        if not agents <= teams[route.team]:
            raise PermissionError("Native workflow references agents outside its routed team")
    workflows: dict[str, Workflow] = {}
    for workflow in definition.workflows:
        graph = admitted[workflow.id]
        nodes: dict[str, Executor] = {}
        for node in graph.nodes:
            if node.kind == "agent":
                nodes[node.id] = AgentExecutor(runtime.agents[str(node.agent)], id=node.id)
            else:
                nodes[node.id] = _OperationExecutor(node.id, registered[str(node.operation)])
        workflows[workflow.id] = _build_native_workflow(
            graph, nodes=nodes, predicates=conditions, checkpoint_storage=checkpoint_storage,
            name=f"{definition.id}.{runtime.snapshot.revision}.{workflow.id}",
        )
    return MappingProxyType(workflows)


def _build_native_workflow(
    graph: WorkflowGraphDefinition, *, nodes: Mapping[str, Executor],
    predicates: Mapping[str, WorkflowPredicate], checkpoint_storage: CheckpointStorage | None,
    name: str,
) -> Workflow:
    builder = WorkflowBuilder(
        start_executor=nodes[graph.start], checkpoint_storage=checkpoint_storage,
        max_iterations=graph.max_iterations, name=name,
        description=graph.description, output_from=[nodes[node_id] for node_id in graph.outputs],
    )
    for edge in graph.edges:
        if edge.kind == "edge":
            condition = _condition(edge.condition, predicates[edge.condition]) if edge.condition is not None else None
            builder.add_edge(nodes[str(edge.source)], nodes[str(edge.target)], condition=condition)
        elif edge.kind == "fan_out":
            builder.add_fan_out_edges(nodes[str(edge.source)], [nodes[node_id] for node_id in edge.targets])
        elif edge.kind == "fan_in":
            builder.add_fan_in_edges([nodes[node_id] for node_id in edge.sources], nodes[str(edge.target)])
        else:
            cases: list[Case | Default] = [
                Case(condition=_condition(case.condition, predicates[case.condition]), target=nodes[case.target])
                for case in edge.cases
            ]
            if edge.default is not None:
                cases.append(Default(nodes[edge.default]))
            builder.add_switch_case_edge_group(nodes[str(edge.source)], cases)
    return builder.build()


def create_model_profile(
    config: RuntimeConfig, *, bindings: FrameworkBindings | None = None,
) -> ModelProfile:
    resolved = bindings if bindings is not None else detect_framework_bindings()
    if config.foundry_endpoint and config.foundry_endpoint.rstrip("/").endswith("/openai/v1"):
        return ModelProfile("openai", _construct_openai_client(bindings=resolved, config=config))
    if config.foundry_endpoint:
        client = _construct_foundry_client(bindings=resolved, config=config)
        if client is not None:
            return ModelProfile("foundry", client)
    if not config.allow_mock_model:
        raise RuntimeError("Model profile unavailable and mock runtime disabled")
    return ModelProfile("mock", {"type": "mock_foundry_client", "model": config.foundry_model})


def bootstrap_organization(
    snapshot: DefinitionSnapshot,
    *,
    model_profiles: Mapping[str, ModelProfile],
    default_model_profile: str,
    tool_profiles: Mapping[str, ToolProfile] | None = None,
    bindings: FrameworkBindings | None = None,
    state_dir: Path | None = None,
) -> OrganizationRuntime:
    models = dict(model_profiles)
    tools = dict(tool_profiles or {})
    for name in (*models, *tools):
        TypeAdapter(Identifier).validate_python(name)
    if default_model_profile not in models:
        raise ValueError("Default model profile is not registered")
    definition = snapshot.definition
    selected_models: dict[str, str] = {}
    selected_tools: dict[str, str | None] = {}
    for agent in definition.agents:
        model_name = agent.model_profile or default_model_profile
        if model_name not in models:
            raise ValueError(f"Unknown model profile for agent {agent.id}: {model_name}")
        if agent.tool_profile is not None:
            if agent.tool_profile not in tools:
                raise ValueError(f"Unknown tool profile for agent {agent.id}: {agent.tool_profile}")
            if tools[agent.tool_profile].role != agent.role:
                raise PermissionError(f"Tool profile role does not match agent {agent.id}")
        selected_models[agent.id] = model_name
        selected_tools[agent.id] = agent.tool_profile
    resolved = bindings if bindings is not None else detect_framework_bindings()
    if any(models[name].mode != "mock" for name in selected_models.values()):
        if resolved.agent_class is None:
            raise RuntimeError("Native Agent unavailable for configured model profiles")
    templates = {spec.role: spec for spec in default_agent_specs()}
    handles: dict[str, Any] = {}
    modes: dict[str, str] = {}
    for agent in definition.agents:
        profile = models[selected_models[agent.id]]
        tool_profile = tools[agent.tool_profile] if agent.tool_profile is not None else None
        agent_state = (
            state_dir / definition.id / snapshot.revision / agent.id
            if state_dir is not None else None
        )
        spec = AgentSpec(
            role=agent.role, name=agent.id,
            instructions=templates[agent.role].instructions + "\n\nConfigured guidance:\n" + agent.instructions,
        )
        handles[agent.id] = build_agent_handle(
            spec, client=profile.client,
            agent_class=resolved.agent_class if profile.mode != "mock" else None,
            tools=tool_profile.tools if tool_profile and agent.role is not AgentRole.DEVELOPER else (),
            developer_state_dir=agent_state,
        )
        modes[agent.id] = profile.mode
    return OrganizationRuntime(
        snapshot=snapshot, agents=MappingProxyType(handles),
        model_profiles=MappingProxyType(selected_models), tool_profiles=MappingProxyType(selected_tools),
        modes=MappingProxyType(modes),
    )