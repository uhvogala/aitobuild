"""Human approvals go through admit_task(Human) and keep their behavior: planning and correction tasks
(which automatic sources may never admit) are still admitted by a person via the dispatcher and the endpoint."""
import pytest
from fastapi.testclient import TestClient

import aitobuild.app as app_module
from aitobuild.dispatcher import DispatcherAgent

SHA = "a" * 40
CASES = {
    "planning": ({"task_id": "pm-planning-1"}, {"action": "opened"}),
    "correction": ({"task_id": "published-correction-1",
                    "issue_context": {"repository": "fixture/widgets", "repository_id": 101, "issue_id": 7, "issue_number": 7,
                                      "title": "t", "body": "b", "base_branch": "main",
                                      "base_revision": SHA}},
                   {"correction_from_review": "dp-review", "published_review": {"preview_id": "dp-1", "head_sha": SHA}}),
}


def _bundle(overrides: dict[str, object]) -> dict[str, object]:
    seed = DispatcherAgent(require_developer_preview=True).create_developer_preview(
        dedupe_key="seed", github_event="issues", action="opened", body={"issue": {"number": 1}})
    base = {key: value for key, value in seed.bundle_payload.items() if key != "issue_context"}
    return base | overrides


@pytest.mark.parametrize("case", sorted(CASES))
def test_dispatcher_human_admits_planning_and_correction(case: str) -> None:
    dispatcher = DispatcherAgent(require_developer_preview=True)
    bundle, source = CASES[case]
    preview = dispatcher.developer_preview_registry.create_or_get(dedupe_key=case, bundle_payload=_bundle(bundle),
                                                                   source_payload=source)
    approved = dispatcher.approve_developer_preview(preview.preview_id)
    assert approved is not None and approved.approved and approved.approval_policy is None


@pytest.mark.parametrize("case", sorted(CASES))
def test_endpoint_human_admits_planning_and_correction(case: str, test_config, monkeypatch) -> None:
    made: list[DispatcherAgent] = []

    class Capturing(DispatcherAgent):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(app_module, "DispatcherAgent", Capturing)
    with TestClient(app_module.create_app(test_config)) as http:
        bundle, source = CASES[case]
        preview = made[0].developer_preview_registry.create_or_get(dedupe_key=case, bundle_payload=_bundle(bundle),
                                                                    source_payload=source)
        response = http.post("/internal/developer/preview/approve", headers={"X-Internal-Token": "internal-test-token"},
                             json={"preview_id": preview.preview_id})
        assert response.status_code == 200, response.text
        assert made[0].developer_preview_registry.get(preview.preview_id).approved
