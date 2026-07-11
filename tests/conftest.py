"""Shared test helpers."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


class IngestStore:
    """Sync mirror of FakeSink's cursor semantics, for the HTTP server."""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.append_calls = 0

    def ensure(self, b: dict) -> dict:
        s = self.sessions.get(b["session_id"])
        return {
            "session_id": b["session_id"],
            "last_round_id": s["last"] if s else None,
            "round_count": s["count"] if s else 0,
        }

    def append(self, b: dict) -> dict:
        self.append_calls += 1
        sid = b["session_id"]
        s = self.sessions.get(sid)
        last = s["last"] if s else None
        if last != b.get("expected_prev_round_id"):
            return {"status": "conflict", "session_id": sid, "actual_last_round_id": last}
        rounds = b.get("rounds") or []
        if not rounds:
            return {"status": "ok", "session_id": sid, "new_last_round_id": last,
                    "appended_count": 0, "round_count": s["count"] if s else 0}
        if not s:
            s = self.sessions[sid] = {"last": None, "rounds": [], "count": 0}
        s["rounds"].extend(rounds)
        s["count"] += len(rounds)
        s["last"] = rounds[-1]["round_id"]
        return {"status": "ok", "session_id": sid, "new_last_round_id": s["last"],
                "appended_count": len(rounds), "round_count": s["count"]}


class _IngestHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        store = self.server.store  # type: ignore[attr-defined]
        if self.path.endswith("/ensure"):
            resp = store.ensure(body)
        elif self.path.endswith("/append"):
            resp = store.append(body)
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silence
        pass


@contextmanager
def run_ingest_server():
    """Start a real local HTTP server speaking the ingest contract
    (/v3/sessions/ensure + /append). Yields (base_url, store)."""
    store = IngestStore()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _IngestHandler)
    server.store = store  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", store
    finally:
        server.shutdown()
        server.server_close()


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
