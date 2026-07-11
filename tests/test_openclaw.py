import asyncio

from collectbase.checkpoint import CheckpointStore
from collectbase.engine import Engine
from collectbase.workers.openclaw import OpenclawWorker
from conftest import FakeSink


class FakeOpenclaw(OpenclawWorker):
    """OpenclawWorker with the HTTP layer replaced by an in-memory store.
    ``state`` holds the remote sessions + rounds; ``etags`` drives change
    detection so we can prove the engine short-circuits on an unchanged
    ETag and re-syncs when it bumps."""

    def __init__(self, state, **kw):
        super().__init__(location="https://openclaw.test", **kw)
        self._state = state

    def _get_json(self, path, params=None):
        if path == "/sessions":
            return {"sessions": [
                {"id": sid, "etag": s["etag"], "created_at": s.get("created_at", ""), "cwd": s.get("cwd")}
                for sid, s in self._state.items()
            ]}
        sid = path.rsplit("/", 1)[1]
        rounds = self._state[sid]["rounds"]
        after = (params or {}).get("after")
        if after:
            ids = [r["id"] for r in rounds]
            rounds = rounds[ids.index(after) + 1:] if after in ids else rounds
        return {"rounds": rounds}


def test_list_remote_and_fetch_shapes():
    state = {"s1": {"etag": "v1", "cwd": "/p", "rounds": [
        {"id": "r1", "role": "human", "ts": "t1", "content": [{"type": "text", "text": "hi"}]},
        {"id": "r2", "role": "assistant", "ts": "t2", "content": [{"type": "text", "text": "yo"}]},
    ]}}
    w = FakeOpenclaw(state)
    probes = list(w.list_remote())
    assert len(probes) == 1 and probes[0].session_id == "s1" and probes[0].sha256 == "v1"
    rounds = list(w.fetch("s1", None))
    assert [r.round_id for r in rounds] == ["r1", "r2"]
    assert rounds[0].content[0].text == "hi"


def test_engine_syncs_polls_and_etag_short_circuits(tmp_path):
    async def go():
        state = {"s1": {"etag": "v1", "rounds": [
            {"id": "r1", "role": "human", "content": [{"type": "text", "text": "hi"}]},
        ]}}
        sink = FakeSink()
        w = FakeOpenclaw(state, poll="30s")
        ckpt = await CheckpointStore.open(tmp_path / "sync.db")
        engine = Engine([w], sink, ckpt)

        # First pass imports r1.
        await engine._sync_one_source(w, "s1")
        sid = w.mint_session_id("s1")
        assert [r.round_id for r in sink.sessions[sid]["rounds"]] == ["r1"]

        # Same ETag → short-circuit, no append.
        calls = sink.append_calls
        await engine._sync_one_source(w, "s1")
        assert sink.append_calls == calls

        # New round + bumped ETag → incremental append of r2.
        state["s1"]["rounds"].append({"id": "r2", "role": "assistant", "content": [{"type": "text", "text": "yo"}]})
        state["s1"]["etag"] = "v2"
        await engine._sync_one_source(w, "s1")
        assert [r.round_id for r in sink.sessions[sid]["rounds"]] == ["r1", "r2"]
        await ckpt.close()

    asyncio.run(go())
