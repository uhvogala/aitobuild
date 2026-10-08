from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from aitobuild.organization import (
    DefinitionStore, FileDefinitionStore, WorkflowGraphDefinition,
    load_organization_definition, parse_organization_definition,
)
from aitobuild.policy import AgentRole


EXAMPLE = Path(__file__).resolve().parents[1] / "config/organization.example.json"


def payload() -> dict:
    return json.loads(EXAMPLE.read_text())


def test_multiple_agents_per_role_and_python_graph_document() -> None:
    definition = load_organization_definition(EXAMPLE)
    assert [agent.id for agent in definition.agents if agent.role is AgentRole.DEVELOPER] == [
        "developer_one", "developer_two",
    ]
    assert definition.teams[0].coordinator == "planner"
    graph = WorkflowGraphDefinition.model_validate(definition.workflows[0].document)
    assert graph.start == "probe" and graph.outputs == ("probe",)


@pytest.mark.parametrize("strategy,target", [("coordinator", None), ("rules", "developer_two"), ("human", None)])
def test_configurable_delegation_definitions(strategy, target) -> None:
    document = payload()
    document["routes"][0]["delegation"].update(strategy=strategy, target_agent=target)
    definition = parse_organization_definition(json.dumps(document))
    assert definition.routes[0].delegation.strategy == strategy
    assert definition.routes[0].delegation.target_agent == target


@pytest.mark.parametrize("case", [
    "skills", "permissions", "role", "blank_prompt", "duplicate_agent", "unknown_member",
    "duplicate_member", "unknown_coordinator", "unknown_team", "unknown_workflow",
    "unknown_delegate", "missing_coordinator", "missing_target", "duplicate_event",
    "boolean_capacity", "boolean_version", "unsupported_version", "empty_nodes",
])
def test_invalid_definitions_fail_closed(case) -> None:
    document = payload()
    if case == "skills":
        document["agents"][0]["skills"] = ["python"]
    elif case == "permissions":
        document["agents"][0]["allowed_actions"] = ["repo_write"]
    elif case == "role":
        document["agents"][0]["role"] = "superuser"
    elif case == "blank_prompt":
        document["agents"][0]["instructions"] = " "
    elif case == "duplicate_agent":
        document["agents"].append(document["agents"][0])
    elif case == "unknown_member":
        document["teams"][0]["members"].append("missing")
    elif case == "duplicate_member":
        document["teams"][0]["members"].append("planner")
    elif case == "unknown_coordinator":
        document["teams"][0]["coordinator"] = "missing"
    elif case == "unknown_team":
        document["routes"][0]["team"] = "missing"
    elif case == "unknown_workflow":
        document["routes"][0]["workflow"] = "missing"
    elif case == "unknown_delegate":
        document["routes"][0]["delegation"]["eligible_agents"] = ["missing"]
    elif case == "missing_coordinator":
        document["teams"][0]["coordinator"] = None
    elif case == "missing_target":
        document["routes"][0]["delegation"]["strategy"] = "rules"
    elif case == "duplicate_event":
        document["routes"][0]["events"] *= 2
    elif case == "boolean_capacity":
        document["agents"][0]["max_concurrent_runs"] = True
    elif case == "boolean_version":
        document["schema_version"] = True
    elif case == "unsupported_version":
        document["schema_version"] = 2
    else:
        document["workflows"][0]["document"]["nodes"] = []
    with pytest.raises(ValueError):
        parse_organization_definition(json.dumps(document))


@pytest.mark.parametrize("content", ['{"schema_version":1,"schema_version":1}', '{"value":NaN}'])
def test_duplicate_keys_and_nonfinite_json_are_rejected(content) -> None:
    with pytest.raises(ValueError):
        parse_organization_definition(content)


def test_revision_roundtrip_restart_and_missing_revision(tmp_path) -> None:
    store: DefinitionStore = FileDefinitionStore(tmp_path)
    snapshot = store.save(load_organization_definition(EXAMPLE))
    assert snapshot.organization_id == "product"
    assert len(snapshot.revision) == 64
    assert FileDefinitionStore(tmp_path).get("product", snapshot.revision) == snapshot
    assert store.get("product", "0" * 64) is None
    with pytest.raises(FrozenInstanceError):
        snapshot.content = "changed"


def test_changed_definition_creates_revision_and_cannot_mutate_previous_snapshot(tmp_path) -> None:
    definition = load_organization_definition(EXAMPLE)
    store = FileDefinitionStore(tmp_path)
    original = store.save(definition)
    definition.workflows[0].document["description"] = "Updated workflow"
    updated = store.save(definition)
    assert original.revision != updated.revision
    assert "description" not in original.definition.workflows[0].document
    restored = original.definition
    restored.workflows[0].document["description"] = "Changed detached copy"
    assert "description" not in original.definition.workflows[0].document
    assert store.get("product", original.revision) == original
    assert store.get("product", updated.revision) == updated


def test_canonical_revision_and_concurrent_idempotent_save(tmp_path) -> None:
    document = payload()
    definition = parse_organization_definition(json.dumps(document, indent=4, sort_keys=True))
    with ThreadPoolExecutor(max_workers=4) as executor:
        snapshots = list(executor.map(
            lambda _: FileDefinitionStore(tmp_path).save(definition), range(8),
        ))
    assert all(snapshot == snapshots[0] for snapshot in snapshots)
    assert list((tmp_path / "product").glob("*.json")) == [
        tmp_path / "product" / f"{snapshots[0].revision}.json",
    ]
    assert FileDefinitionStore(tmp_path).save(load_organization_definition(EXAMPLE)) == snapshots[0]


def test_corrupt_snapshot_is_rejected_not_replaced(tmp_path) -> None:
    store = FileDefinitionStore(tmp_path)
    definition = load_organization_definition(EXAMPLE)
    snapshot = store.save(definition)
    path = tmp_path / "product" / f"{snapshot.revision}.json"
    corrupted = snapshot.content.replace("developer_one", "developer_other")
    path.write_text(corrupted)
    with pytest.raises(ValueError, match="revision"):
        store.get("product", snapshot.revision)
    with pytest.raises(ValueError, match="revision"):
        store.save(definition)
    assert path.read_text() == corrupted


def test_interrupted_save_keeps_previous_revision_and_retries_safely(tmp_path, monkeypatch) -> None:
    store = FileDefinitionStore(tmp_path)
    original = store.save(load_organization_definition(EXAMPLE))
    document = payload()
    document["agents"][0]["instructions"] = "Updated coordinator prompt."
    updated = parse_organization_definition(json.dumps(document))

    def fail_sync(descriptor: int) -> None:
        raise OSError("Interrupted snapshot write")

    with monkeypatch.context() as patch:
        patch.setattr("aitobuild.durable_files.os.fsync", fail_sync)
        with pytest.raises(OSError, match="Interrupted"):
            store.save(updated)
    assert store.get("product", original.revision) == original
    assert len(list((tmp_path / "product").glob("*.json"))) == 1
    snapshot = store.save(updated)
    assert snapshot.revision != original.revision
    assert FileDefinitionStore(tmp_path).get("product", snapshot.revision) == snapshot


@pytest.mark.parametrize("organization_id,revision", [
    ("../outside", "0" * 64), ("product", "../outside"), ("product", "A" * 64),
])
def test_definition_addresses_cannot_escape_store(tmp_path, organization_id, revision) -> None:
    with pytest.raises(ValueError):
        FileDefinitionStore(tmp_path).get(organization_id, revision)


def test_action_documents_are_not_organization_graphs() -> None:
    document = payload()
    document["workflows"][0]["document"] = {
        "actions": [{"kind": "SendActivity", "activity": "unsupported"}],
    }
    with pytest.raises(ValueError):
        parse_organization_definition(json.dumps(document))