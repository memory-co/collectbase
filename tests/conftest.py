"""Shared fixtures + scenario staging helpers.

The tests read as stories: stage a real jsonl tree the way an agent tool
would write it, start the collector, do something upstream (append a
round, drop a new session file), and poll until the effect lands in the
sink — the same path a running daemon takes. Builders here keep those
stories short.
"""
from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from collectbase import Collectbase
from collectbase.checkpoint import CheckpointStore
from collectbase.engine import Engine
from collectbase.format import (
    AppendRoundsRequest,
    AppendRoundsResponse,
    EnsureSessionRequest,
    EnsureSessionResponse,
)


# ─── a memory system that only counts (in-process) ───────────────────


class FakeSink:
    """The memory side, in memory. Faithful to the two things the engine
    leans on: a stable per-session cursor, and optimistic-concurrency
    appends keyed off ``expected_prev_round_id`` (a mismatch → conflict).
    """

    def __init__(self):
        self.sessions: dict[str, dict] = {}  # sid → {last, rounds, count}
        self.append_calls = 0

    def rounds_of(self, session_id: str) -> list[str]:
        """round_ids stored for a session — the thing scenarios assert on."""
        s = self.sessions.get(session_id)
        return [r.round_id for r in s["rounds"]] if s else []

    async def ensure_session(self, req: EnsureSessionRequest) -> EnsureSessionResponse:
        s = self.sessions.get(req.session_id)
        return EnsureSessionResponse(
            session_id=req.session_id,
            last_round_id=s["last"] if s else None,
            round_count=s["count"] if s else 0,
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
                status="ok", session_id=req.session_id, new_last_round_id=server_last,
                appended_count=0, round_count=s["count"] if s else 0,
            )
        if not s:
            s = self.sessions[req.session_id] = {"last": None, "rounds": [], "count": 0}
        s["rounds"].extend(req.rounds)
        s["count"] += len(req.rounds)
        s["last"] = req.rounds[-1].round_id
        return AppendRoundsResponse(
            status="ok", session_id=req.session_id, new_last_round_id=s["last"],
            appended_count=len(req.rounds), round_count=s["count"],
        )


@pytest.fixture
def sink() -> FakeSink:
    return FakeSink()


# ─── the same memory system, over real HTTP ──────────────────────────


class IngestStore:
    """FakeSink's cursor logic, sync, behind an HTTP handler."""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.append_calls = 0

    def rounds_of(self, session_id: str) -> list[str]:
        s = self.sessions.get(session_id)
        return [r["round_id"] for r in s["rounds"]] if s else []

    def ensure(self, b: dict) -> dict:
        s = self.sessions.get(b["session_id"])
        return {"session_id": b["session_id"], "last_round_id": s["last"] if s else None,
                "round_count": s["count"] if s else 0}

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
        store: IngestStore = self.server.store  # type: ignore[attr-defined]
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

    def log_message(self, *args):
        pass


@contextmanager
def run_ingest_server():
    """A real local HTTP server speaking the ingest contract
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


# ─── staging upstream data ───────────────────────────────────────────


def cc_msg(uuid, text, *, mtype="user", parent=None, ts="2026-07-11T00:00:00Z",
           cwd="/work/proj", **extra):
    """One Claude Code transcript line (as it lands on disk)."""
    msg = {
        "type": mtype, "uuid": uuid, "parentUuid": parent, "timestamp": ts,
        "cwd": cwd, "message": {"content": [{"type": "text", "text": text}]},
    }
    msg.update(extra)
    return msg


def codex_env(typ, payload, ts="2026-05-24T00:00:00Z"):
    """One Codex envelope line."""
    return {"timestamp": ts, "type": typ, "payload": payload}


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))


def append_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def stage_claude_session(root: Path, project: str, name: str, msgs: list[dict]) -> Path:
    """Write a Claude Code session file under ``root/project/name.jsonl``."""
    path = root / project / f"{name}.jsonl"
    write_jsonl(path, msgs)
    return path


# A tiny but realistic Claude Code chat: human → assistant → tool_result
# echo-back, preceded by a non-round "summary" line the worker must skip.
CLAUDE_CHAT = [
    {"type": "summary", "summary": "a chat"},
    cc_msg("u1", "hello", mtype="user", ts="2026-07-11T00:00:00Z"),
    cc_msg("a1", "hi back", mtype="assistant", parent="u1", ts="2026-07-11T00:00:01Z"),
    cc_msg("t1", "", mtype="user", ts="2026-07-11T00:00:02Z",
           toolUseResult={"ok": True},
           message={"content": [{"type": "tool_result", "content": "file data"}]}),
]


# ─── polling helpers (live scenarios) ────────────────────────────────


async def wait_for_phase(cb: Collectbase, target: str, *, timeout: float = 5.0):
    """Poll the engine until it reaches ``target`` phase (e.g. 'watching')."""
    for _ in range(int(timeout / 0.05)):
        if cb.engine.phase == target:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"phase never reached {target!r}; last={cb.engine.phase!r}")


async def wait_until(predicate, *, timeout: float = 5.0, what: str = "condition"):
    """Poll ``predicate()`` until truthy. For live fs-watch scenarios where
    the effect arrives a debounce-window after the file changes."""
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{what} did not happen within {timeout}s")


@pytest.fixture
async def collector(tmp_path):
    """Factory: open + start a Collectbase, wait until it's watching, and
    auto-close it at teardown. Call it with (workers, sink) inside a test.

        cb = await collector([ClaudeCodeWorker(location=...)], sink)
    """
    started: list[Collectbase] = []

    async def _make(workers, a_sink, *, debounce_ms: int = 50) -> Collectbase:
        cb = await Collectbase.open(
            checkpoint_dir=tmp_path / "collect", sink=a_sink,
            workers=workers, debounce_ms=debounce_ms,
        )
        await cb.start()
        await wait_for_phase(cb, "watching")
        started.append(cb)
        return cb

    yield _make
    for cb in started:
        await cb.close()


@pytest.fixture
async def checkpoints(tmp_path):
    store = await CheckpointStore.open(tmp_path / "cursors.db")
    yield store
    await store.close()
