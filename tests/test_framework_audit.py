from __future__ import annotations

from aitobuild.framework_audit import CapabilityDecision, ensure_decision_exists, phase1_capability_matrix


def test_capability_matrix_has_required_entries() -> None:
    names = {item.capability for item in phase1_capability_matrix()}
    assert "agent_roles_and_instructions" in names
    assert "chat_client_transport" in names
    assert "meeting_bootstrap" in names


def test_ensure_decision_exists_returns_expected_value() -> None:
    decision = ensure_decision_exists("chat_client_transport")
    assert decision is CapabilityDecision.REUSE_NATIVE
