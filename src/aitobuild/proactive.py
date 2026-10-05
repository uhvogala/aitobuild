"""Proactive Architect scan workflow and structured output contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(slots=True, frozen=True)
class ScanFinding:
    code: str
    severity: str
    summary: str


@dataclass(slots=True, frozen=True)
class IssueProposal:
    title: str
    body: str
    labels: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class ArchitectScanOutcome:
    status: str
    scan_time: str
    findings: tuple[ScanFinding, ...]
    issue_proposals: tuple[IssueProposal, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "scan_time": self.scan_time,
            "findings": [asdict(item) for item in self.findings],
            "issue_proposals": [asdict(item) for item in self.issue_proposals],
        }


class ArchitectScanRunner:
    """Deterministic scan logic for proactive architect checks in Phase 1."""

    def run(self, payload: dict[str, Any]) -> ArchitectScanOutcome:
        findings: list[ScanFinding] = []
        proposals: list[IssueProposal] = []

        lint_warnings = _as_non_negative_int(payload.get("lint_warnings"))
        if lint_warnings > 0:
            findings.append(
                ScanFinding(
                    code="ARCH_LINT_WARNINGS",
                    severity="medium",
                    summary=f"Detected {lint_warnings} lint warning(s) in proactive scan context.",
                )
            )
            proposals.append(
                IssueProposal(
                    title="Reduce lint warning backlog",
                    body=(
                        "Proactive Architect scan detected lint warning debt. "
                        "Please triage and resolve warning clusters to prevent quality drift."
                    ),
                    labels=("architect-scan", "quality"),
                )
            )

        test_failures = _as_non_negative_int(payload.get("test_failures"))
        if test_failures > 0:
            findings.append(
                ScanFinding(
                    code="ARCH_TEST_FAILURES",
                    severity="high",
                    summary=f"Detected {test_failures} failing test(s) in proactive scan context.",
                )
            )
            proposals.append(
                IssueProposal(
                    title="Stabilize failing tests surfaced by proactive scan",
                    body=(
                        "Proactive Architect scan found unstable or failing tests. "
                        "Investigate root cause and restore deterministic test behavior."
                    ),
                    labels=("architect-scan", "test-stability"),
                )
            )

        if bool(payload.get("missing_docs")):
            findings.append(
                ScanFinding(
                    code="ARCH_MISSING_DOCS",
                    severity="low",
                    summary="Documentation coverage gaps were flagged by proactive scan.",
                )
            )
            proposals.append(
                IssueProposal(
                    title="Improve architecture and module documentation",
                    body=(
                        "Proactive Architect scan flagged missing documentation for core behaviors. "
                        "Add concise docs for design intent, constraints, and extension points."
                    ),
                    labels=("architect-scan", "documentation"),
                )
            )

        if not findings:
            findings.append(
                ScanFinding(
                    code="ARCH_HEALTHY_BASELINE",
                    severity="info",
                    summary="No critical architecture concerns detected in this proactive scan.",
                )
            )

        status = "issues_found" if proposals else "healthy"
        return ArchitectScanOutcome(
            status=status,
            scan_time=datetime.now(tz=UTC).isoformat(),
            findings=tuple(findings),
            issue_proposals=tuple(proposals),
        )


def _as_non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return max(0, value)
    return 0
