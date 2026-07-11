"""End-to-end over real HTTP — proves collectbase runs standalone against
any server speaking the ingest contract, with no in-process coupling.

A stdlib HTTP server (tests/conftest.run_ingest_server) plays the memory
side; the full stack (Collectbase facade → engine → HttpSink → wire)
drives it. No memory.talk import anywhere.
"""
import asyncio

from collectbase import Collectbase, HttpSink
from collectbase.checkpoint import CheckpointStore
from collectbase.engine import Engine
from collectbase.workers.claude_code import ClaudeCodeWorker
from conftest import CC_RECORDS, append_jsonl, run_ingest_server, write_jsonl


def test_engine_over_http_import_then_incremental(tmp_path):
    async def go():
        with run_ingest_server() as (base_url, store):
            root = tmp_path / "projects"
            f = root / "proj-x" / "s.jsonl"
            write_jsonl(f, CC_RECORDS)

            sink = HttpSink(base_url)
            ckpt = await CheckpointStore.open(tmp_path / "sync.db")
            engine = Engine([ClaudeCodeWorker(location=str(root))], sink, ckpt)

            # Import over the wire.
            await engine._run_backfill()
            assert len(store.sessions) == 1
            sess = next(iter(store.sessions.values()))
            assert [r["round_id"] for r in sess["rounds"]] == ["u1", "a1", "t1"]

            # Unchanged → sha short-circuit, no HTTP append.
            calls = store.append_calls
            await engine._run_backfill()
            assert store.append_calls == calls

            # Incremental append over the wire.
            append_jsonl(f, [{"type": "assistant", "uuid": "a2",
                              "message": {"content": [{"type": "text", "text": "more"}]}}])
            await engine._run_backfill()
            sess = next(iter(store.sessions.values()))
            assert [r["round_id"] for r in sess["rounds"]] == ["u1", "a1", "t1", "a2"]

            await ckpt.close()
            await sink.aclose()

    asyncio.run(go())


def test_facade_lifecycle_over_http(tmp_path):
    """Collectbase.open → start (backfill to 'watching') → close, with a
    real HttpSink. close() must also shut the sink's transport."""
    async def go():
        with run_ingest_server() as (base_url, store):
            root = tmp_path / "projects"
            write_jsonl(root / "p" / "s.jsonl", CC_RECORDS)

            sink = HttpSink(base_url)
            cb = await Collectbase.open(
                checkpoint_dir=tmp_path / "collect",
                sink=sink,
                workers=[ClaudeCodeWorker(location=str(root))],
            )
            await cb.start()
            for _ in range(200):
                if cb.engine.phase == "watching":
                    break
                await asyncio.sleep(0.02)
            assert cb.engine.phase == "watching"
            st = await cb.status()
            assert st["checkpoints"] == 1
            assert len(store.sessions) == 1

            await cb.close()
            # sink transport closed by facade.close()
            assert sink._client.is_closed

    asyncio.run(go())
