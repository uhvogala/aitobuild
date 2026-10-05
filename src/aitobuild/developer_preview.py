"""Developer bundle preview registry and approval lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4


@dataclass(slots=True, frozen=True)
class DeveloperPreview:
    preview_id: str
    dedupe_key: str
    bundle_payload: dict[str, object]
    source_payload: dict[str, object]
    approved: bool
    created_at: datetime
    approved_at: datetime | None = None


class DeveloperPreviewRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, DeveloperPreview] = {}
        self._by_dedupe: dict[str, str] = {}

    def create_or_get(
        self,
        *,
        dedupe_key: str,
        bundle_payload: dict[str, object],
        source_payload: dict[str, object],
    ) -> DeveloperPreview:
        existing = self.get_by_dedupe(dedupe_key)
        if existing is not None:
            return existing

        preview = DeveloperPreview(
            preview_id=f"dp-{uuid4()}",
            dedupe_key=dedupe_key,
            bundle_payload=bundle_payload,
            source_payload=source_payload,
            approved=False,
            created_at=datetime.now(tz=UTC),
        )
        self._by_id[preview.preview_id] = preview
        self._by_dedupe[preview.dedupe_key] = preview.preview_id
        return preview

    def approve(self, preview_id: str) -> DeveloperPreview | None:
        preview = self._by_id.get(preview_id)
        if preview is None:
            return None
        if preview.approved:
            return preview

        approved_preview = replace(preview, approved=True, approved_at=datetime.now(tz=UTC))
        self._by_id[preview_id] = approved_preview
        self._by_dedupe[approved_preview.dedupe_key] = preview_id
        return approved_preview

    def get(self, preview_id: str) -> DeveloperPreview | None:
        return self._by_id.get(preview_id)

    def get_by_dedupe(self, dedupe_key: str) -> DeveloperPreview | None:
        preview_id = self._by_dedupe.get(dedupe_key)
        if preview_id is None:
            return None
        return self._by_id.get(preview_id)

    def is_approved_for_dedupe(self, dedupe_key: str) -> bool:
        preview = self.get_by_dedupe(dedupe_key)
        return bool(preview and preview.approved)

    def list_previews(self, *, pending_only: bool, limit: int) -> tuple[DeveloperPreview, ...]:
        previews = tuple(self._by_id.values())
        if pending_only:
            previews = tuple(preview for preview in previews if not preview.approved)
        if limit < len(previews):
            previews = previews[-limit:]
        return previews
