"""OpenclawWorker — the polling (non-file) tier.

No files on disk: the engine polls ``list_remote`` and pulls ``fetch``,
and per-session change detection rides the ETag (probe.sha256). These
scenarios replace the HTTP layer with an in-memory remote so we can drive
the ETag lifecycle deterministically — bump the etag, expect a re-sync;
leave it, expect a short-circuit.
"""
from __future__ import annotations

from collectbase.engine import Engine
from collectbase.workers.openclaw import OpenclawWorker


class FakeOpenclaw(OpenclawWorker):
    """OpenclawWorker with ``_get_json`` served from an in-memory dict.
    ``remote`` maps session id → {etag, cwd?, rounds:[{id,role,ts,content}]}."""

    def __init__(self, remote, **kw):
        super().__init__(location="https://openclaw.test", **kw)
        self.remote = remote

    def _get_json(self, path, params=None):
        if path == "/sessions":
            return {"sessions": [
                {"id": sid, "etag": s["etag"], "created_at": s.get("created_at", ""), "cwd": s.get("cwd")}
                for sid, s in self.remote.items()
            ]}
        sid = path.rsplit("/", 1)[1]
        rounds = self.remote[sid]["rounds"]
        after = (params or {}).get("after")
        if after:
            ids = [r["id"] for r in rounds]
            rounds = rounds[ids.index(after) + 1:] if after in ids else rounds
        return {"rounds": rounds}


def _remote_one_session():
    return {"s1": {"etag": "v1", "cwd": "/p", "rounds": [
        {"id": "r1", "role": "human", "ts": "t1", "content": [{"type": "text", "text": "hi"}]},
        {"id": "r2", "role": "assistant", "ts": "t2", "content": [{"type": "text", "text": "yo"}]},
    ]}}


# ────────── shape of what the worker returns ──────────


class TestRemoteShapes:

    def test_list_remote_maps_sessions_with_etag_as_the_cursor(self):
        worker = FakeOpenclaw(_remote_one_session())
        probes = list(worker.list_remote())
        assert len(probes) == 1
        assert probes[0].session_id == "s1"
        assert probes[0].sha256 == "v1"  # etag drives change detection
        assert probes[0].metadata["cwd"] == "/p"

    def test_fetch_normalizes_rounds(self):
        worker = FakeOpenclaw(_remote_one_session())
        rounds = list(worker.fetch("s1", after_round_id=None))
        assert [r.round_id for r in rounds] == ["r1", "r2"]
        assert rounds[0].content[0].text == "hi"


# ────────── ETag lifecycle through the engine ──────────


class TestEtagLifecycle:

    async def test_unchanged_etag_short_circuits_new_rounds_resync(self, checkpoints, sink):
        remote = {"s1": {"etag": "v1", "rounds": [
            {"id": "r1", "role": "human", "content": [{"type": "text", "text": "hi"}]},
        ]}}
        worker = FakeOpenclaw(remote, poll="30s")
        engine = Engine([worker], sink, checkpoints)
        sid = worker.mint_session_id("s1")

        # First poll imports r1.
        await engine._sync_one_source(worker, "s1")
        assert sink.rounds_of(sid) == ["r1"]

        # Same etag → the engine skips before it even asks the sink.
        calls = sink.append_calls
        await engine._sync_one_source(worker, "s1")
        assert sink.append_calls == calls

        # New round + bumped etag → only the new round is pulled.
        remote["s1"]["rounds"].append({"id": "r2", "role": "assistant",
                                       "content": [{"type": "text", "text": "yo"}]})
        remote["s1"]["etag"] = "v2"
        await engine._sync_one_source(worker, "s1")
        assert sink.rounds_of(sid) == ["r1", "r2"]
