"""Shared test helpers."""
from __future__ import annotations

import json
from pathlib import Path

from collectbase.format import (
    AppendRoundsRequest,
    AppendRoundsResponse,
    EnsureSessionRequest,
    EnsureSessionResponse,
)


class FakeSink:
    """In-memory ingest that mirrors the server's cursor semantics:
    optimistic-concurrency append keyed by ``expected_prev_round_id``,
    stable per-session cursor. Satisfies the ``Sink`` protocol."""

    def __init__(self):
        # sid -> {"last": str|None, "rounds": list, "count": int}
        self.sessions: dict[str, dict] = {}
        self.append_calls = 0

    async def ensure_session(self, req: EnsureSessionRequest) -> EnsureSessionResponse:
        s = self.sessions.get(req.session_id)
        if not s:
            return EnsureSessionResponse(session_id=req.session_id, last_round_id=None, round_count=0)
        return EnsureSessionResponse(
            session_id=req.session_id, last_round_id=s["last"], round_count=s["count"]
        )

    async def append_rounds(self, req: AppendRoundsRequest) -> AppendRoundsResponse:
        self.append_calls += 1
        s = self.sessions.get(req.session_id)
        server_last = s["last"] if s else None
        if server_last != req.expected_prev_round_id:
            return AppendRoundsResponse(
                status="conflict", session_id=req.session_id, actual_last_round_id=server_last
            )
        if not req.rounds:
            return AppendRoundsResponse(
                status="ok",
                session_id=req.session_id,
                new_last_round_id=server_last,
                appended_count=0,
                round_count=s["count"] if s else 0,
            )
        if not s:
            s = self.sessions[req.session_id] = {"last": None, "rounds": [], "count": 0}
        s["rounds"].extend(req.rounds)
        s["count"] += len(req.rounds)
        s["last"] = req.rounds[-1].round_id
        return AppendRoundsResponse(
            status="ok",
            session_id=req.session_id,
            new_last_round_id=s["last"],
            appended_count=len(req.rounds),
            round_count=s["count"],
        )


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# A small, realistic claude-code transcript: human → assistant(+tool_use)
# → tool_result echo-back, plus a non-round "summary" line to skip.
CC_RECORDS = [
    {"type": "summary", "summary": "a chat"},
    {
        "type": "user",
        "uuid": "u1",
        "timestamp": "2026-07-11T00:00:00Z",
        "cwd": "/proj",
        "message": {"content": [{"type": "text", "text": "hi"}]},
    },
    {
        "type": "assistant",
        "uuid": "a1",
        "parentUuid": "u1",
        "timestamp": "2026-07-11T00:00:01Z",
        "message": {
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "tool_use", "name": "Read", "input": {"file": "x"}},
            ]
        },
    },
    {
        "type": "user",
        "uuid": "t1",
        "timestamp": "2026-07-11T00:00:02Z",
        "toolUseResult": {"ok": True},
        "message": {"content": [{"type": "tool_result", "content": "file data"}]},
    },
]
