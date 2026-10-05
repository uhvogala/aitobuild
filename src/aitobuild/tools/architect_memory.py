"""Simple durable memory store for Architect architectural decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any
from uuid import uuid4
from filelock import FileLock


@dataclass(slots=True, frozen=True)
class ArchitectMemoryRecord:
    memory_id: str
    topic: str
    content: str
    created_at: str
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "topic": self.topic,
            "content": self.content,
            "created_at": self.created_at,
            "tags": list(self.tags),
        }


class ArchitectMemoryStore:
    """Append-only JSONL memory with keyword query (scaffolding, not a vector DB)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(path) + ".lock", timeout=10)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")

    def record(self, *, topic: str, content: str, tags: tuple[str, ...] = ()) -> ArchitectMemoryRecord:
        cleaned_topic = topic.strip()
        cleaned_content = content.strip()
        if not cleaned_topic:
            raise ValueError("topic must be non-empty")
        if not cleaned_content:
            raise ValueError("content must be non-empty")
        if any(token in cleaned_content.lower() for token in ("password", "api_key", "secret", "token=")):
            raise ValueError("memory content must not include credentials or secrets")
        item = ArchitectMemoryRecord(
            memory_id=f"amem-{uuid4().hex[:12]}",
            topic=cleaned_topic,
            content=cleaned_content,
            created_at=datetime.now(tz=UTC).isoformat(),
            tags=tuple(tag.strip() for tag in tags if tag.strip()),
        )
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item.to_dict(), ensure_ascii=True) + "\n")
        return item

    def query(self, *, query: str, limit: int = 5) -> tuple[ArchitectMemoryRecord, ...]:
        needle = query.strip().lower()
        if not needle:
            raise ValueError("query must be non-empty")
        bound = max(1, min(limit, 50))
        records = self._load()
        scored: list[tuple[int, ArchitectMemoryRecord]] = []
        tokens = [token for token in needle.replace(",", " ").split() if token]
        for record in records:
            haystack = f"{record.topic}\n{record.content}\n{' '.join(record.tags)}".lower()
            score = sum(1 for token in tokens if token in haystack)
            if score > 0 or needle in haystack:
                scored.append((score if score > 0 else 1, record))
        scored.sort(key=lambda item: (-item[0], item[1].created_at))
        return tuple(record for _, record in scored[:bound])

    def _load(self) -> list[ArchitectMemoryRecord]:
        with self._lock:
            text = self.path.read_text(encoding="utf-8")
        records: list[ArchitectMemoryRecord] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            records.append(
                ArchitectMemoryRecord(
                    memory_id=str(raw["memory_id"]),
                    topic=str(raw["topic"]),
                    content=str(raw["content"]),
                    created_at=str(raw["created_at"]),
                    tags=tuple(raw.get("tags") or ()),
                )
            )
        return records
