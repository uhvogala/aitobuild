from __future__ import annotations

from aitobuild.proactive import ArchitectScanRunner


def test_architect_scan_runner_reports_healthy_without_signals() -> None:
    runner = ArchitectScanRunner()
    outcome = runner.run({})

    assert outcome.status == "healthy"
    assert len(outcome.issue_proposals) == 0


def test_architect_scan_runner_generates_issue_proposals_for_risks() -> None:
    runner = ArchitectScanRunner()
    outcome = runner.run({"lint_warnings": 3, "test_failures": 1, "missing_docs": True})

    assert outcome.status == "issues_found"
    assert len(outcome.issue_proposals) >= 2
    assert any(finding.code == "ARCH_TEST_FAILURES" for finding in outcome.findings)
