from pathlib import Path

from collectbase.workers.claude_code import ClaudeCodeWorker
from conftest import CC_RECORDS, append_jsonl, write_jsonl


def _worker(tmp_path: Path) -> ClaudeCodeWorker:
    return ClaudeCodeWorker(location=str(tmp_path / "projects"))


def test_to_round_classifies_user_buckets():
    w = ClaudeCodeWorker(location="/x")
    human = w.to_round({"type": "user", "uuid": "u1", "message": {"content": [{"type": "text", "text": "hi"}]}})
    assert (human.role, human.speaker) == ("human", "user")

    tool = w.to_round({
        "type": "user", "uuid": "t1", "toolUseResult": {"ok": 1},
        "message": {"content": [{"type": "tool_result", "content": "x"}]},
    })
    assert (tool.role, tool.speaker) == ("tool", "tool")

    meta = w.to_round({"type": "user", "uuid": "m1", "isMeta": True, "message": {"content": [{"type": "text", "text": "note"}]}})
    assert (meta.role, meta.speaker) == ("system", "harness")

    slash = w.to_round({"type": "user", "uuid": "s1", "message": {"content": [{"type": "text", "text": "<command-name>/foo"}]}})
    assert (slash.role, slash.speaker) == ("system", "harness")


def test_to_round_skips_non_message_rows():
    w = ClaudeCodeWorker(location="/x")
    assert w.to_round({"type": "summary", "summary": "s"}) is None


def test_to_round_maps_content_blocks():
    w = ClaudeCodeWorker(location="/x")
    r = w.to_round(CC_RECORDS[2])  # assistant with text + tool_use
    assert r.role == "assistant"
    assert [b.type for b in r.content] == ["text", "tool_use"]
    assert r.content[1].input == '{"file": "x"}'


def test_list_sources_and_probe(tmp_path):
    w = _worker(tmp_path)
    f = tmp_path / "projects" / "proj-x" / "sess-uuid.jsonl"
    write_jsonl(f, CC_RECORDS)
    probes = list(w.list_sources())
    assert len(probes) == 1
    p = probes[0]
    assert p.session_id == "sess-uuid"
    assert p.created_at == "2026-07-11T00:00:00Z"
    assert p.metadata["project"] == "proj-x"
    assert len(p.sha256) == 64


def test_read_after_full_then_incremental(tmp_path):
    w = _worker(tmp_path)
    f = tmp_path / "projects" / "proj-x" / "s.jsonl"
    write_jsonl(f, CC_RECORDS)

    full = w.read_after(str(f), after_round_id=None)
    assert [r.round_id for r in full.rounds] == ["u1", "a1", "t1"]
    assert full.next_line_offset == 4  # 4 physical lines incl. summary

    # Append a new assistant round; incremental read from cursor t1.
    append_jsonl(f, [{"type": "assistant", "uuid": "a2", "message": {"content": [{"type": "text", "text": "more"}]}}])
    # Stale hint (4) points at the summary line, not t1 → forces a scan.
    inc = w.read_after(str(f), after_round_id="t1", hint_line_offset=4)
    assert [r.round_id for r in inc.rounds] == ["a2"]
    assert inc.next_line_offset == 5


def test_read_after_valid_hint_fast_path(tmp_path):
    w = _worker(tmp_path)
    f = tmp_path / "projects" / "p" / "s.jsonl"
    # No summary line: line 3 (1-indexed) carries t1, so hint=3 is valid.
    write_jsonl(f, CC_RECORDS[1:])
    append_jsonl(f, [{"type": "assistant", "uuid": "a2", "message": {"content": [{"type": "text", "text": "m"}]}}])
    inc = w.read_after(str(f), after_round_id="t1", hint_line_offset=3)
    assert [r.round_id for r in inc.rounds] == ["a2"]


def test_mint_session_id_namespaces_by_endpoint():
    a = ClaudeCodeWorker(location="/loc/a")
    b = ClaudeCodeWorker(location="/loc/b")
    assert a.mint_session_id("019e-abcd-e0f4") != b.mint_session_id("019e-abcd-e0f4")
    assert a.mint_session_id("019e-abcd-e0f4").endswith("-e0f4")
