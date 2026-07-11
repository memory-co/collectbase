import asyncio

from collectbase.checkpoint import CheckpointStore


def test_checkpoint_roundtrip(tmp_path):
    async def go():
        store = await CheckpointStore.open(tmp_path / "sync.db")
        assert await store.get("claude-code", "/loc", "s1") is None
        await store.upsert("claude-code", "/loc", "s1", "sha1", "r1", 10, "2026-07-11T00:00:00Z")
        got = await store.get("claude-code", "/loc", "s1")
        assert got["sha256"] == "sha1"
        assert got["last_round_id"] == "r1"
        assert got["line_offset"] == 10
        # Same upstream id at a different location is a distinct row.
        await store.upsert("claude-code", "/other", "s1", "sha2", "r2", 5, "2026-07-11T00:00:01Z")
        assert (await store.get("claude-code", "/other", "s1"))["sha256"] == "sha2"
        assert await store.count() == 2
        # Upsert overwrites in place.
        await store.upsert("claude-code", "/loc", "s1", "sha1b", "r9", 20, "2026-07-11T00:00:02Z")
        assert (await store.get("claude-code", "/loc", "s1"))["last_round_id"] == "r9"
        assert await store.count() == 2
        await store.close()

    asyncio.run(go())
