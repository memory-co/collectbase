"""Standalone over real HTTP — collectbase against a memory it only knows
by the wire contract.

A stdlib HTTP server (conftest.run_ingest_server) plays the memory side,
speaking /v3/sessions/ensure + /append. The full stack drives it through
the ``HttpSink``, with no memory.talk anywhere in the process. These are
the same backfill / live-watch stories as test_engine, but proving they
survive a JSON round-trip over a socket.
"""
from __future__ import annotations

from collectbase import Collectbase, HttpSink
from collectbase.workers.claude_code import ClaudeCodeWorker
from conftest import (
    CLAUDE_CHAT,
    append_jsonl,
    cc_msg,
    run_ingest_server,
    stage_claude_session,
    wait_for_phase,
    wait_until,
)


class TestOverHttp:

    async def test_prepared_session_lands_over_the_wire(self, tmp_path):
        with run_ingest_server() as (base_url, store):
            root = tmp_path / "claude"
            stage_claude_session(root, "proj", "chat", CLAUDE_CHAT)
            worker = ClaudeCodeWorker(location=str(root))
            sink = HttpSink(base_url)

            cb = await Collectbase.open(checkpoint_dir=tmp_path / "collect",
                                        sink=sink, workers=[worker], debounce_ms=50)
            await cb.start()
            await wait_for_phase(cb, "watching")

            sid = worker.mint_session_id("chat")
            assert store.rounds_of(sid) == ["u1", "a1", "t1"]
            await cb.close()

    async def test_appended_round_is_pushed_live_over_http(self, tmp_path):
        with run_ingest_server() as (base_url, store):
            root = tmp_path / "claude"
            f = stage_claude_session(root, "proj", "chat", CLAUDE_CHAT)
            worker = ClaudeCodeWorker(location=str(root))
            sink = HttpSink(base_url)

            cb = await Collectbase.open(checkpoint_dir=tmp_path / "collect",
                                        sink=sink, workers=[worker], debounce_ms=50)
            await cb.start()
            await wait_for_phase(cb, "watching")
            sid = worker.mint_session_id("chat")

            append_jsonl(f, [cc_msg("a2", "more", mtype="assistant", parent="t1")])
            await wait_until(lambda: "a2" in store.rounds_of(sid),
                             what="appended round pushed over HTTP")
            assert store.rounds_of(sid) == ["u1", "a1", "t1", "a2"]
            await cb.close()

    async def test_close_shuts_the_http_transport(self, tmp_path):
        """The facade owns the sink's lifecycle: close() must also close
        the HttpSink's client, so nothing leaks."""
        with run_ingest_server() as (base_url, _store):
            root = tmp_path / "claude"
            stage_claude_session(root, "proj", "chat", CLAUDE_CHAT)
            sink = HttpSink(base_url)
            cb = await Collectbase.open(checkpoint_dir=tmp_path / "collect", sink=sink,
                                        workers=[ClaudeCodeWorker(location=str(root))],
                                        debounce_ms=50)
            await cb.start()
            await wait_for_phase(cb, "watching")
            await cb.close()
            assert sink._client.is_closed
