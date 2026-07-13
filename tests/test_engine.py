"""Engine end-to-end — the stories a running collector lives through.

Everything drives the real ``Collectbase`` facade against a ``FakeSink``:
start it, let backfill run, then change things upstream (append a round,
drop a new session file) and poll until the effect reaches the sink —
the same path the daemon takes. The lower-level cursor mechanics
(resume, hint validation) are covered per-worker in test_claude_code /
test_codex; here we care about behavior across the whole loop.
"""
from __future__ import annotations

from pathlib import Path

from collectbase import Collectbase
from collectbase.engine import Engine
from collectbase.format import EnsureSessionResponse, ReadAfterResult
from collectbase.worker import Worker
from collectbase.workers.claude_code import ClaudeCodeWorker
from conftest import (
    CLAUDE_CHAT,
    append_jsonl,
    cc_msg,
    stage_claude_session,
    wait_for_phase,
    wait_until,
)


def _claude(root: Path) -> ClaudeCodeWorker:
    return ClaudeCodeWorker(location=str(root))


# ────────── backfill on startup ──────────


class TestBackfill:

    async def test_prepared_session_is_imported_on_startup(self, tmp_path, sink, collector):
        """A transcript already on disk when the collector starts gets
        ingested during backfill — by the time we're watching, it's there."""
        root = tmp_path / "claude"
        stage_claude_session(root, "proj", "chat", CLAUDE_CHAT)
        worker = _claude(root)

        await collector([worker], sink)

        sid = worker.mint_session_id("chat")
        assert sink.rounds_of(sid) == ["u1", "a1", "t1"]  # summary line skipped

    async def test_restart_reimports_nothing_thanks_to_checkpoint(self, tmp_path):
        """The cursor lives in collectbase's own sync.db. On restart the
        engine re-scans, sees the source sha is unchanged, and short-
        circuits before ever touching the sink — even a brand-new server
        gets nothing, because 'already synced' is collectbase's own fact."""
        from conftest import FakeSink

        root = tmp_path / "claude"
        stage_claude_session(root, "proj", "chat", CLAUDE_CHAT)
        sid = _claude(root).mint_session_id("chat")

        first = FakeSink()
        cb1 = await Collectbase.open(checkpoint_dir=tmp_path / "collect", sink=first,
                                     workers=[_claude(root)], debounce_ms=50)
        await cb1.start()
        await wait_for_phase(cb1, "watching")
        assert first.rounds_of(sid) == ["u1", "a1", "t1"]  # imported first time
        await cb1.close()

        # Restart against a FRESH (empty) server: the checkpoint short-
        # circuit means we don't re-push a thing.
        second = FakeSink()
        cb2 = await Collectbase.open(checkpoint_dir=tmp_path / "collect", sink=second,
                                     workers=[_claude(root)], debounce_ms=50)
        await cb2.start()
        await wait_for_phase(cb2, "watching")
        assert second.append_calls == 0
        assert second.sessions == {}
        await cb2.close()


# ────────── live fs-watch ──────────


class TestLiveWatch:

    async def test_appended_round_is_picked_up(self, tmp_path, sink, collector):
        """Append a round to a live session file; the watcher debounces,
        reads only what's new, and the round shows up in the sink."""
        root = tmp_path / "claude"
        session_file = stage_claude_session(root, "proj", "chat", CLAUDE_CHAT)
        worker = _claude(root)
        await collector([worker], sink)
        sid = worker.mint_session_id("chat")
        assert sink.rounds_of(sid) == ["u1", "a1", "t1"]

        append_jsonl(session_file, [cc_msg("a2", "one more", mtype="assistant", parent="t1")])

        await wait_until(lambda: "a2" in sink.rounds_of(sid),
                         what="appended round a2 to be ingested")
        assert sink.rounds_of(sid) == ["u1", "a1", "t1", "a2"]

    async def test_new_session_file_is_discovered(self, tmp_path, sink, collector):
        """A session file created after the collector is already watching
        gets discovered and imported live — not just the ones present at
        startup."""
        root = tmp_path / "claude"
        stage_claude_session(root, "proj", "chat", CLAUDE_CHAT)
        worker = _claude(root)
        await collector([worker], sink)

        stage_claude_session(root, "proj", "later", [
            cc_msg("x1", "brand new session", mtype="user"),
        ])
        later_sid = worker.mint_session_id("later")
        await wait_until(lambda: bool(sink.rounds_of(later_sid)),
                         what="newly-created session to be discovered")
        assert sink.rounds_of(later_sid) == ["x1"]


# ────────── conflict reconciliation ──────────


class TestConflictReconciliation:

    async def test_stale_cursor_reconciles_against_server(self, tmp_path, checkpoints):
        """Another writer got a round in first, but our ensure read the
        server a beat too early and reported an empty cursor. The append
        then conflicts; the engine re-reads after the server's *actual*
        cursor and retries once, so nothing is lost or double-written."""
        from conftest import FakeSink

        root = tmp_path / "claude"
        stage_claude_session(root, "proj", "chat", CLAUDE_CHAT)
        worker = _claude(root)
        sid = worker.mint_session_id("chat")

        sink = FakeSink()
        # Server already holds u1 (the other writer beat us to it).
        u1 = worker.read_after(str(root / "proj/chat.jsonl"), after_round_id=None).rounds[0]
        sink.sessions[sid] = {"last": "u1", "rounds": [u1], "count": 1}

        # First ensure lies (reports empty) → forces the stale-cursor path.
        real_ensure = sink.ensure_session
        lied = {"done": False}

        async def ensure_stale_once(req):
            if not lied["done"]:
                lied["done"] = True
                return EnsureSessionResponse(session_id=req.session_id, last_round_id=None)
            return await real_ensure(req)

        sink.ensure_session = ensure_stale_once

        engine = Engine([worker], sink, checkpoints)
        await engine._run_backfill()

        # a1, t1 landed cleanly on top of the pre-existing u1 — no dup, no gap.
        assert sink.rounds_of(sid) == ["u1", "a1", "t1"]


# ────────── error isolation ──────────


class _PoisonWorker(Worker):
    """A worker that blows up the moment the engine enumerates it."""

    source = "poison"
    default_location = "/nonexistent"

    def watch_roots(self):
        return []

    def list_sources(self):
        raise RuntimeError("boom")

    def probe(self, source_id):
        return None

    def read_after(self, source_id, after_round_id, hint_line_offset=0):
        return ReadAfterResult()


class TestErrorIsolation:

    async def test_one_bad_worker_does_not_sink_the_rest(self, tmp_path, sink, collector):
        """A poisoned worker raising during backfill is isolated by the
        per-worker try/except: the healthy claude-code session still
        imports, and the failure surfaces in status.recent."""
        root = tmp_path / "claude"
        stage_claude_session(root, "proj", "chat", CLAUDE_CHAT)
        good = _claude(root)

        cb = await collector([_PoisonWorker(), good], sink)

        # Healthy worker unaffected.
        assert sink.rounds_of(good.mint_session_id("chat")) == ["u1", "a1", "t1"]
        # Poison worker's error is recorded, not swallowed.
        errors = [e for e in cb.engine.status()["recent"] if e.get("event") == "error"]
        assert any("boom" in (e.get("error") or "") for e in errors), errors
