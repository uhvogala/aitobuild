# Agents Notes

## Agent Framework Docs (Check First)

Reference docs:
- https://github.com/microsoft/agent-framework/tree/main/python/samples

Working rule:
- If any Agent Framework behavior or API is unclear, check the docs above before implementing custom logic.
- Prefer native framework capabilities first; only build custom code when there is a confirmed gap.

## Project Basics

Project:
- Name: aitobuild
- Current phase: Phase 1 foundation
- Purpose: agentic product-team framework with PM, Architect, and Developer roles

Current foundation scope:
- FastAPI ingress with webhook and internal trigger endpoints
- Unified trigger/event pipeline and deduplication
- Dispatcher routing for webhook, proactive scan, and meeting bootstrap events
- Runtime bootstrap for FoundryChatClient with mock fallback mode
- Scheduler scaffolding for proactive Architect scans and meeting trigger events
- Policy guardrails for role permissions and approval-gated repo writes

Key endpoints:
- GET /health
- POST /webhook
- POST /internal/triggers

Quality gates:
- pytest
- ruff
- mypy

## Dependency Management (uv)

- Use `uv` for all Python dependency operations in this repo.
- Add packages with `uv add <package>`.
- Update installed environment with `uv sync` after dependency changes.
- Do not use ad-hoc `pip install` for project dependencies.
