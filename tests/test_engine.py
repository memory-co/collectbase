import asyncio
from pathlib import Path

from collectbase.checkpoint import CheckpointStore
from collectbase.engine import Engine
from collectbase.workers.claude_code import ClaudeCodeWorker
from conftest import CC_RECORDS, FakeSink, append_jsonl, write_jsonl


def _setup(tmp_path: Path):
    root = tmp_path / "projects"
    f = root / "proj-x" / "s.jsonl"
    write_jsonl(f, CC_RECORDS)
    return root, f


async def _engine(tmp_path, root, sink):
    ckpt = await CheckpointStore.open(tmp_path / "sync.db")
    worker = ClaudeCodeWorker(location=str(root))
    return Engine([worker], sink, ckpt), ckpt


def test_backfill_imports_then_incremental_then_skips(tmp_path):
    async def go():
        root, f = _setup(tmp_path)
        sink = FakeSink()
        engine, ckpt = await _engine(tmp_path, root, sink)

        # First backfill: imports the 3 rounds as one session.
        await engine._run_backfill()
        assert len(sink.sessions) == 1
        sess = next(iter(sink.sessions.values()))
        assert [r.round_id for r in sess["rounds"]] == ["u1", "a1", "t1"]
        assert engine._totals["_total"]["imported"] == 1

        # Second pass, nothing changed: sha short-circuit → no append call.
        calls_before = sink.append_calls
        await engine._run_backfill()
        assert sink.append_calls == calls_before
        assert engine._totals["_total"]["skipped"] >= 1

        # Append a round upstream, re-sync: only the new round is appended.
        append_jsonl(f, [{"type": "assistant", "uuid": "a2", "message": {"content": [{"type": "text", "text": "more"}]}}])
        await engine._run_backfill()
        sess = next(iter(sink.sessions.values()))
        assert [r.round_id for r in sess["rounds"]] == ["u1", "a1", "t1", "a2"]
        assert engine._totals["_total"]["appended"] == 1

        await ckpt.close()

    asyncio.run(go())


def test_checkpoint_persists_cursor(tmp_path):
    async def go():
        root, f = _setup(tmp_path)
        sink = FakeSink()
        engine, ckpt = await _engine(tmp_path, root, sink)
        await engine._run_backfill()

        worker = engine.workers[0]
        row = await ckpt.get(worker.source, worker.location, "s")
        assert row is not None
        assert row["last_round_id"] == "t1"
        assert row["line_offset"] == 4
        await ckpt.close()

    asyncio.run(go())


def test_conflict_retry_reconciles(tmp_path):
    """If the server is ahead of what ensure reported, the append conflicts;
    the engine re-reads after the actual cursor and retries once."""
    async def go():
        root, f = _setup(tmp_path)
        sink = FakeSink()
        engine, ckpt = await _engine(tmp_path, root, sink)
        worker = engine.workers[0]

        # Pre-seed the server as if u1 was already ingested, but hand the
        # engine a stale ensure (last=None) via a one-shot wrapper so the
        # first append is built with expected_prev=None → conflict.
        sid = worker.mint_session_id("s")
        sink.sessions[sid] = {"last": "u1", "rounds": [type("R", (), {"round_id": "u1"})()], "count": 1}

        real_ensure = sink.ensure_session
        stale_done = {"v": False}

        async def stale_once(req):
            if not stale_done["v"]:
                stale_done["v"] = True
                from collectbase.format import EnsureSessionResponse
                return EnsureSessionResponse(session_id=req.session_id, last_round_id=None)
            return await real_ensure(req)

        sink.ensure_session = stale_once
        await engine._run_backfill()

        # After conflict-retry, a1 and t1 land on top of the pre-existing u1.
        assert [r.round_id for r in sink.sessions[sid]["rounds"]] == ["u1", "a1", "t1"]
        await ckpt.close()

    asyncio.run(go())


def test_full_lifecycle_start_close(tmp_path):
    """Smoke: real start()/stop() (observer thread + tasks) reaches
    'watching' and ingests via backfill without raising."""
    async def go():
        root, f = _setup(tmp_path)
        sink = FakeSink()
        engine, ckpt = await _engine(tmp_path, root, sink)
        await engine.start()
        for _ in range(200):
            if engine.phase == "watching":
                break
            await asyncio.sleep(0.02)
        assert engine.phase == "watching"
        assert len(sink.sessions) == 1
        await engine.stop()
        assert engine.running is False
        await ckpt.close()

    asyncio.run(go())
