"""Minimal context-matched patch parsing and apply helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


PatchAction = Literal["add", "update", "delete"]


@dataclass(slots=True, frozen=True)
class PatchHunk:
    pre_context: tuple[str, ...]
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    post_context: tuple[str, ...]
    line_kinds: tuple[tuple[str, str], ...] = ()


@dataclass(slots=True, frozen=True)
class PatchOperation:
    action: PatchAction
    path: str
    hunks: tuple[PatchHunk, ...] = ()
    add_lines: tuple[str, ...] = ()


def parse_patch_document(patch_text: str) -> tuple[PatchOperation, ...]:
    lines = patch_text.splitlines()
    lines = _trim_blank_lines(lines)
    lines = _strip_code_fence(lines)
    lines = _trim_blank_lines(lines)
    if not lines:
        raise ValueError("patch content is empty")

    if lines[0] == "*** Begin Patch":
        body = lines[1:-1] if len(lines) >= 2 and lines[-1] == "*** End Patch" else lines[1:]
        body = _trim_blank_lines(body)
        if body and body[0].startswith("--- "):
            # Support unified diff wrapped in custom envelope markers.
            return _parse_unified_patch_document(body)

        # Tolerate missing closing marker; parse remainder as custom patch body.
        return _parse_custom_patch_document(body)

    if lines[0].startswith("*** Update File: ") or lines[0].startswith("*** Add File: ") or lines[0].startswith(
        "*** Delete File: "
    ):
        # Support custom patch body without explicit Begin/End wrapper.
        return _parse_custom_patch_document(lines)

    if lines[0].startswith("--- "):
        return _parse_unified_patch_document(lines)

    raise ValueError(
        "unsupported patch format; expected custom envelope ('*** Begin Patch') or unified diff ('---/+++')"
    )


def _parse_custom_patch_document(body: list[str]) -> tuple[PatchOperation, ...]:
    if not body:
        raise ValueError("patch body is empty")

    operations: list[PatchOperation] = []
    index = 0
    while index < len(body):
        header = body[index]
        if header.startswith("*** Add File: "):
            path = header[len("*** Add File: ") :].strip()
            index += 1
            add_lines: list[str] = []
            while index < len(body) and not body[index].startswith("*** "):
                line = body[index]
                add_lines.append(line[1:] if line.startswith("+") else line)
                index += 1
            operations.append(PatchOperation(action="add", path=path, add_lines=tuple(add_lines)))
            continue

        if header.startswith("*** Delete File: "):
            path = header[len("*** Delete File: ") :].strip()
            index += 1
            operations.append(PatchOperation(action="delete", path=path))
            continue

        if header.startswith("*** Update File: "):
            path = header[len("*** Update File: ") :].strip()
            index += 1
            update_lines: list[str] = []
            while index < len(body) and not body[index].startswith("*** "):
                update_lines.append(body[index])
                index += 1

            hunks = _parse_update_hunks(update_lines)
            operations.append(PatchOperation(action="update", path=path, hunks=hunks))
            continue

        raise ValueError(f"unrecognized patch header: {header}")

    return tuple(operations)


def _parse_unified_patch_document(lines: list[str]) -> tuple[PatchOperation, ...]:
    operations: list[PatchOperation] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            raise ValueError("unified diff file section must start with '--- '")
        old_path_raw = lines[index][4:].strip().split("\t", 1)[0]
        index += 1

        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise ValueError("unified diff file section must include '+++ '")
        new_path_raw = lines[index][4:].strip().split("\t", 1)[0]
        index += 1

        if old_path_raw == "/dev/null" or new_path_raw == "/dev/null":
            raise ValueError("unified diff add/delete sections are not supported; use custom patch envelope")

        old_path = _strip_unified_prefix(old_path_raw)
        new_path = _strip_unified_prefix(new_path_raw)
        if old_path != new_path:
            raise ValueError("unified diff path mismatch between --- and +++")

        hunks: list[PatchHunk] = []
        while index < len(lines) and not lines[index].startswith("--- "):
            if not lines[index].startswith("@@"):
                raise ValueError("unified diff hunk must start with '@@'")
            index += 1

            old_block: list[str] = []
            new_block: list[str] = []
            while index < len(lines) and not lines[index].startswith("@@") and not lines[index].startswith("--- "):
                line = lines[index]
                if line.startswith(" "):
                    value = line[1:]
                    old_block.append(value)
                    new_block.append(value)
                elif line.startswith("-"):
                    old_block.append(line[1:])
                elif line.startswith("+"):
                    new_block.append(line[1:])
                elif line.startswith("\\ No newline at end of file"):
                    pass
                else:
                    raise ValueError("invalid unified diff hunk line; expected ' ', '+', '-', or no-newline marker")
                index += 1

            if old_block == new_block:
                raise ValueError("unified diff hunk must include at least one change")

            # Store full old/new hunk blocks as direct replace ranges.
            hunks.append(
                PatchHunk(
                    pre_context=(),
                    old_lines=tuple(old_block),
                    new_lines=tuple(new_block),
                    post_context=(),
                )
            )

        if not hunks:
            raise ValueError("unified diff file section must contain at least one hunk")
        operations.append(PatchOperation(action="update", path=new_path, hunks=tuple(hunks)))

    return tuple(operations)


def apply_update_hunks(content: str, hunks: tuple[PatchHunk, ...]) -> str:
    if not hunks:
        raise ValueError("update operation must contain at least one hunk")

    lines = content.splitlines()
    had_trailing_newline = content.endswith("\n")

    for hunk in hunks:
        pattern = list(hunk.pre_context) + list(hunk.old_lines) + list(hunk.post_context)
        replacement = list(hunk.pre_context) + list(hunk.new_lines) + list(hunk.post_context)
        if not pattern:
            raise ValueError("hunk must include context or old lines")

        matches = _find_subsequence_matches(lines=lines, pattern=pattern)
        if len(matches) != 1:
            normalized_pre_context = _strip_one_leading_space(hunk.pre_context)
            normalized_post_context = _strip_one_leading_space(hunk.post_context)
            if normalized_pre_context != hunk.pre_context or normalized_post_context != hunk.post_context:
                normalized_pattern = list(normalized_pre_context) + list(hunk.old_lines) + list(normalized_post_context)
                normalized_replacement = (
                    list(normalized_pre_context) + list(hunk.new_lines) + list(normalized_post_context)
                )
                normalized_matches = _find_subsequence_matches(lines=lines, pattern=normalized_pattern)
                if len(normalized_matches) == 1:
                    pattern = normalized_pattern
                    replacement = normalized_replacement
                    matches = normalized_matches

        if len(matches) != 1 and len(hunk.new_lines) > len(hunk.old_lines):
            # Tolerate a common model near-miss where some inserted lines are
            # emitted without '+' prefixes, often as a shared trailing suffix.
            shared_suffix_len = _common_suffix_length(hunk.old_lines, hunk.new_lines)
            if 0 < shared_suffix_len < len(hunk.old_lines):
                insertion_pattern = (
                    list(hunk.pre_context)
                    + list(hunk.old_lines[:-shared_suffix_len])
                    + list(hunk.post_context)
                )
                insertion_replacement = (
                    list(hunk.pre_context)
                    + list(hunk.new_lines)
                    + list(hunk.post_context)
                )
                insertion_matches = _find_subsequence_matches(lines=lines, pattern=insertion_pattern)
                if len(insertion_matches) == 1:
                    pattern = insertion_pattern
                    replacement = insertion_replacement
                    matches = insertion_matches

        if len(matches) != 1 and hunk.line_kinds:
            repaired = _repair_missing_plus_in_addition_block(
                line_kinds=hunk.line_kinds,
                current_lines=lines,
            )
            if repaired is not None:
                repaired_old, repaired_new = repaired
                repaired_pattern = list(hunk.pre_context) + repaired_old + list(hunk.post_context)
                repaired_replacement = list(hunk.pre_context) + repaired_new + list(hunk.post_context)
                repaired_matches = _find_subsequence_matches(lines=lines, pattern=repaired_pattern)
                if len(repaired_matches) == 1:
                    pattern = repaired_pattern
                    replacement = repaired_replacement
                    matches = repaired_matches

        if len(matches) != 1 and not hunk.old_lines and hunk.post_context:
            # Tolerate a common model near-miss where inserted lines after '+'
            # are emitted without '+' prefixes and end up parsed as post-context.
            insertion_pattern = list(hunk.pre_context)
            insertion_replacement = list(hunk.pre_context) + list(hunk.new_lines) + list(hunk.post_context)
            insertion_matches = _find_subsequence_matches(lines=lines, pattern=insertion_pattern)
            if len(insertion_matches) == 1:
                pattern = insertion_pattern
                replacement = insertion_replacement
                matches = insertion_matches

        if len(matches) != 1:
            missing_plus_hint = _build_missing_plus_hint(lines=lines, hunk=hunk)
            detail_suffix = f" Hint: {missing_plus_hint}" if missing_plus_hint is not None else ""
            raise ValueError(
                "hunk context does not uniquely match current file contents"
                if matches
                else f"hunk context not found in current file contents{detail_suffix}"
            )

        match_index = matches[0]
        replace_start = match_index
        replace_end = replace_start + len(pattern)
        lines = lines[:replace_start] + replacement + lines[replace_end:]

    result = "\n".join(lines)
    if had_trailing_newline:
        return f"{result}\n"
    return result


def _parse_update_hunks(lines: list[str]) -> tuple[PatchHunk, ...]:
    if not lines:
        raise ValueError("update operation body is empty")

    hunks: list[PatchHunk] = []
    index = 0
    while index < len(lines):
        header = lines[index]
        if not header.startswith("@@"):
            raise ValueError("each update hunk must start with '@@'")
        index += 1
        strip_unified_context_prefix = header.strip() != "@@"

        hunk_lines: list[str] = []
        while index < len(lines) and not lines[index].startswith("@@"):
            hunk_lines.append(lines[index])
            index += 1

        if not strip_unified_context_prefix and _looks_like_unified_context_markers(hunk_lines):
            strip_unified_context_prefix = True

        hunks.append(
            _parse_single_hunk(
                hunk_lines,
                strip_unified_context_prefix=strip_unified_context_prefix,
            )
        )

    return tuple(hunks)


def _parse_single_hunk(
    lines: list[str],
    *,
    strip_unified_context_prefix: bool,
) -> PatchHunk:
    old_block: list[str] = []
    new_block: list[str] = []
    line_kinds: list[tuple[str, str]] = []
    saw_change = False
    for line in lines:
        if line.startswith("\\ No newline at end of file"):
            continue

        if line.startswith("-"):
            saw_change = True
            value = line[1:]
            line_kinds.append(("-", value))
            old_block.append(value)
            continue

        if line.startswith("+"):
            saw_change = True
            value = line[1:]
            line_kinds.append(("+", value))
            new_block.append(value)
            continue

        context_line = line[1:] if strip_unified_context_prefix and line.startswith(" ") else line
        line_kinds.append((" ", context_line))

        old_block.append(context_line)
        new_block.append(context_line)

    if not saw_change:
        raise ValueError("hunk must include at least one '-' or '+' line")

    if not old_block:
        raise ValueError("insertion hunk must include context")

    return PatchHunk(
        pre_context=(),
        old_lines=tuple(old_block),
        new_lines=tuple(new_block),
        post_context=(),
        line_kinds=tuple(line_kinds),
    )


def _repair_missing_plus_in_addition_block(
    *,
    line_kinds: tuple[tuple[str, str], ...],
    current_lines: list[str],
) -> tuple[list[str], list[str]] | None:
    old_lines: list[str] = []
    new_lines: list[str] = []
    repaired = False

    for index, (kind, value) in enumerate(line_kinds):
        if kind == "-":
            old_lines.append(value)
            continue

        if kind == "+":
            new_lines.append(value)
            continue

        # Context line that doesn't exist in current file and is surrounded by
        # additions is likely a missed '+' prefix from model output.
        if (
            value not in current_lines
            and _has_addition_marker_around(line_kinds=line_kinds, index=index, direction=-1)
            and _has_addition_marker_around(line_kinds=line_kinds, index=index, direction=1)
        ):
            new_lines.append(value)
            repaired = True
            continue

        old_lines.append(value)
        new_lines.append(value)

    if not repaired:
        return None
    return old_lines, new_lines


def _has_addition_marker_around(
    *,
    line_kinds: tuple[tuple[str, str], ...],
    index: int,
    direction: int,
) -> bool:
    step = 1 if direction >= 0 else -1
    cursor = index + step
    while 0 <= cursor < len(line_kinds):
        kind, value = line_kinds[cursor]
        if kind == "+":
            return True
        if kind == "-":
            return False
        if value.strip():
            return False
        cursor += step
    return False


def _find_subsequence_matches(*, lines: list[str], pattern: list[str]) -> list[int]:
    if len(pattern) > len(lines):
        return []

    matches: list[int] = []
    for index in range(0, len(lines) - len(pattern) + 1):
        if lines[index : index + len(pattern)] == pattern:
            matches.append(index)
    return matches


def _strip_one_leading_space(lines: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(line[1:] if line.startswith(" ") else line for line in lines)


def _common_suffix_length(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    max_len = min(len(left), len(right))
    length = 0
    while length < max_len and left[-(length + 1)] == right[-(length + 1)]:
        length += 1
    return length


def _build_missing_plus_hint(*, lines: list[str], hunk: PatchHunk) -> str | None:
    # Common model near-miss: bare custom hunks insert multiline code blocks
    # where only blank separators are prefixed with '+'.
    if len(hunk.new_lines) <= len(hunk.old_lines):
        return None

    missing_nonblank_old_lines = [line for line in hunk.old_lines if line.strip() and line not in lines]
    if not missing_nonblank_old_lines:
        return None

    return (
        "for custom '@@' hunks, every inserted line must start with '+'. "
        "If inserting a multiline block, prefix each new code line with '+', not only blank separators"
    )


def _looks_like_unified_context_markers(lines: list[str]) -> bool:
    context_lines = [
        line
        for line in lines
        if not line.startswith("+")
        and not line.startswith("-")
        and not line.startswith("\\ No newline at end of file")
    ]
    if not context_lines:
        return False

    return all(line.startswith(" ") for line in context_lines)


def _strip_unified_prefix(path: str) -> str:
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _trim_blank_lines(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _strip_code_fence(lines: list[str]) -> list[str]:
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].startswith("```"):
        return lines[1:-1]
    return lines
