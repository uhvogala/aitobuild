"""Mock-first filesystem adapter with role/action checks."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from aitobuild.policy import ActionClass, AgentRole, assert_repo_write_approval, assert_role_action_allowed


class FilesystemAdapter(Protocol):
    def read_text(
        self,
        *,
        role: AgentRole,
        path: Path,
        session_id: str | None = None,
    ) -> str:
        ...

    def write_text(
        self,
        *,
        role: AgentRole,
        path: Path,
        content: str,
        approved: bool,
        require_human_approval_for_repo_writes: bool,
        session_id: str | None = None,
    ) -> None:
        ...


class MockFilesystemAdapter:
    def read_text(self, *, role: AgentRole, path: Path, session_id: str | None = None) -> str:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)
        with path.open(encoding="utf-8", newline="") as stream:
            return stream.read()

    def write_text(
        self,
        *,
        role: AgentRole,
        path: Path,
        content: str,
        approved: bool,
        require_human_approval_for_repo_writes: bool,
        session_id: str | None = None,
    ) -> None:
        assert_role_action_allowed(role, ActionClass.REPO_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        path.write_text(content, encoding="utf-8")
