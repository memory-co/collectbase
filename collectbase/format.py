"""Standard session format — the single shape everything normalizes to.

Two layers of types live here:

  1. **Wire models** (pydantic): ``ContentBlock`` / ``RoundInput`` /
     ``SourceProbe`` / ``ReadAfterResult`` + the ensure/append request &
     response pairs. These are the bytes-on-the-wire contract shared with
     the memory system's ingest endpoints (``/v3/sessions/ensure`` &
     ``/append``). Do not casually reshape them — the peer parses them.

  2. **Author-facing builders**: ``Round(...)`` plus the content-block
     helpers ``Text`` / ``Thinking`` / ``Code`` / ``ToolUse`` /
     ``ToolResult`` / ``Block``. Worker authors call these; they return
     the wire models above so ``to_round`` / ``parse`` can hand results
     straight to the engine. See docs/session-format.md.

The builders return ``None`` for empty content so a worker can write
``[b for b in map(_block, raw) if b]`` and have empty blocks vanish.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field


# ─── wire: content + round ────────────────────────────────────────────


class ContentBlock(BaseModel):
    """One block inside a round's ``content`` array.

    Free-form on purpose: platforms emit text / code / thinking /
    tool_use / tool_result etc. We preserve them verbatim and let
    downstream consumers project as needed. Unknown keys are kept
    (``extra="allow"``).
    """

    type: str
    text: str | None = None
    language: str | None = None
    thinking: str | None = None
    model_config = {"extra": "allow"}


class RoundInput(BaseModel):
    """A single round as accepted by ingest.

    ``index`` is deliberately absent — the server assigns it on first
    write and keeps it stable across re-ingests. Workers supply
    ``round_id`` (the platform / synthesized id) which the server uses
    to align with already-stored rounds. See docs/session-format.md §3.
    """

    round_id: str
    parent_id: str | None = None
    timestamp: str | None = None
    speaker: str | None = None
    role: str | None = None
    content: list[ContentBlock] = Field(default_factory=list)
    is_sidechain: bool = False
    cwd: str | None = None
    usage: dict[str, Any] | None = None


# ─── author-facing builders ───────────────────────────────────────────


def Text(text: str | None) -> ContentBlock | None:
    """Plain text block. Returns ``None`` for empty text."""
    return ContentBlock(type="text", text=text) if text else None


def Thinking(text: str | None) -> ContentBlock | None:
    """Model reasoning / thinking block. ``None`` for empty."""
    return ContentBlock(type="thinking", thinking=text) if text else None


def Code(text: str | None, language: str | None = None) -> ContentBlock | None:
    """Code block. ``None`` for empty."""
    return ContentBlock(type="code", text=text, language=language) if text else None


def ToolUse(name: str, input: Any = "") -> ContentBlock:
    """Tool-call block. ``input`` dicts are JSON-serialized; a ``text``
    projection is attached for full-text search."""
    if isinstance(input, (dict, list)):
        input = json.dumps(input, ensure_ascii=False)
    return ContentBlock(type="tool_use", name=name, input=input, text=f"[{name}] {input}")


def ToolResult(text: Any) -> ContentBlock:
    """Tool-result block. Non-str content is JSON-serialized."""
    if isinstance(text, (dict, list)):
        text = json.dumps(text, ensure_ascii=False)
    return ContentBlock(type="tool_result", text=str(text))


def Block(type: str, **fields: Any) -> ContentBlock:
    """Escape hatch for any block shape not covered above. Kept verbatim
    (``ContentBlock`` allows extra keys)."""
    return ContentBlock(type=type, **fields)


def Round(
    id: str,
    role: str | None = None,
    content: str | Iterable[ContentBlock | None] | None = None,
    *,
    speaker: str | None = None,
    at: str | None = None,
    parent: str | None = None,
    cwd: str | None = None,
    sidechain: bool = False,
    usage: dict[str, Any] | None = None,
) -> RoundInput:
    """Build one normalized round (see docs/session-format.md §1).

    ``content`` accepts a plain ``str`` (wrapped into a single ``Text``
    block) or an iterable of blocks; ``None`` entries are dropped so
    ``map(_block, raw)`` output can be passed straight through.
    ``speaker`` defaults to ``role``.
    """
    if content is None:
        blocks: list[ContentBlock] = []
    elif isinstance(content, str):
        blocks = [b for b in (Text(content),) if b is not None]
    else:
        blocks = [b for b in content if b is not None]
    return RoundInput(
        round_id=id,
        role=role,
        speaker=speaker if speaker is not None else role,
        timestamp=at,
        parent_id=parent,
        content=blocks,
        is_sidechain=sidechain,
        cwd=cwd,
        usage=usage,
    )


# ─── wire: probe + read result ────────────────────────────────────────


class SourceProbe(BaseModel):
    """Light-weight inspection of one upstream artifact.

    Describes "what's the current state of this file/URL right now"
    without yielding round payloads. The engine compares ``sha256``
    against its checkpoint to decide whether to bother reading.
    """

    source_id: str  # adapter-side resource id (abs path, full URL, ...)
    session_id: str  # upstream raw id (no ``sess-`` prefix)
    sha256: str  # whole-artifact content hash (or ETag, ...) for change-detect
    created_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


# Public alias used by worker authors (docs call it ``Probe``).
Probe = SourceProbe


class ReadAfterResult(BaseModel):
    """Rounds strictly after a cursor, with a hint for the next read.

    ``next_line_offset`` is the worker's opaque seek hint (line number
    for jsonl, byte offset / pagination cursor for others) — the engine
    stores it and hands it back on the next call but never interprets it.
    """

    rounds: list[RoundInput] = Field(default_factory=list)
    next_line_offset: int = 0


# ─── wire: ingest (sink) contract ─────────────────────────────────────


class EnsureSessionRequest(BaseModel):
    """Read a session's current ingest cursor without writing. The
    ``session_id`` is the canonical minted id (``sess-<loc8>-<lastseg>``)."""

    session_id: str
    source: str
    location: str = ""
    location_label: str | None = None


class EnsureSessionResponse(BaseModel):
    session_id: str
    last_round_id: str | None = None
    round_count: int = 0


class AppendRoundsRequest(BaseModel):
    """Append new rounds under optimistic concurrency. ``session_id`` is
    the canonical minted id."""

    session_id: str
    source: str
    location: str = ""
    location_label: str | None = None
    expected_prev_round_id: str | None = None
    rounds: list[RoundInput] = Field(default_factory=list)
    created_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class AppendRoundsResponse(BaseModel):
    status: Literal["ok", "conflict"]
    session_id: str
    new_last_round_id: str | None = None
    appended_count: int = 0
    round_count: int = 0
    # Populated on status="conflict" — the server's actual cursor.
    actual_last_round_id: str | None = None
    # Vector-index outcome — an independent axis from append status.
    indexed_count: int = 0
    index_failed_count: int = 0
    index_status: Literal["ok", "partial", "failed"] = "ok"
    index_error: str | None = None
