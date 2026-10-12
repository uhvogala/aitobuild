from datetime import UTC, datetime

from aitobuild.developer_preview import DeveloperPreview, is_planned_child


def _preview(task_id: str, source_payload: dict[str, object]) -> DeveloperPreview:
    return DeveloperPreview(preview_id="p", dedupe_key="d", bundle_payload={"task_id": task_id},
                            source_payload=source_payload, approved=False, created_at=datetime.now(UTC))


def test_planned_prefix_without_planning_assignment_marker_is_planned_child() -> None:
    # The task-ID prefix alone must qualify; no planning_assignment marker is present.
    assert is_planned_child(_preview("planned-issue-" + "0" * 64, {}))
    assert is_planned_child(_preview("dependent-issue-" + "0" * 64, {"action": "opened"}))
    assert not is_planned_child(_preview("ISSUE-1", {}))
