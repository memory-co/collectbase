"""The cursor store — "how far into each source have I read".

Connector state, keyed by (source, location, upstream id). The scenarios
that matter: a cursor round-trips, the same upstream id at two locations
stays independent (US + EU can't clobber each other), and re-syncing a
source overwrites its row in place rather than piling up.
"""
from __future__ import annotations


async def test_a_cursor_round_trips(checkpoints):
    assert await checkpoints.get("claude-code", "/loc", "s1") is None
    await checkpoints.upsert("claude-code", "/loc", "s1", "sha1", "r1", 10, "2026-07-11T00:00:00Z")
    got = await checkpoints.get("claude-code", "/loc", "s1")
    assert (got["sha256"], got["last_round_id"], got["line_offset"]) == ("sha1", "r1", 10)


async def test_same_id_at_two_locations_is_two_rows(checkpoints):
    await checkpoints.upsert("claude-code", "/loc", "s1", "shaA", "rA", 1, "2026-07-11T00:00:00Z")
    await checkpoints.upsert("claude-code", "/other", "s1", "shaB", "rB", 2, "2026-07-11T00:00:01Z")
    assert (await checkpoints.get("claude-code", "/loc", "s1"))["sha256"] == "shaA"
    assert (await checkpoints.get("claude-code", "/other", "s1"))["sha256"] == "shaB"
    assert await checkpoints.count() == 2


async def test_resync_overwrites_in_place(checkpoints):
    await checkpoints.upsert("claude-code", "/loc", "s1", "sha1", "r1", 10, "2026-07-11T00:00:00Z")
    await checkpoints.upsert("claude-code", "/loc", "s1", "sha2", "r9", 20, "2026-07-11T00:00:02Z")
    assert (await checkpoints.get("claude-code", "/loc", "s1"))["last_round_id"] == "r9"
    assert await checkpoints.count() == 1
