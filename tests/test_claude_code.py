"""ClaudeCodeWorker — reading ~/.claude/projects/**/*.jsonl.

The genuinely claude-code-specific behavior is the ``type:"user"`` row,
which on disk is a four-way bucket (real human input / tool_result echo-
back / harness meta / slash-command artifacts). Most of these scenarios
pin that classification down; the rest confirm the file becomes a session
with a resumable cursor.
"""
from __future__ import annotations

from pathlib import Path

from collectbase.workers.claude_code import ClaudeCodeWorker
from conftest import CLAUDE_CHAT, cc_msg, stage_claude_session, write_jsonl


def _worker(root: Path) -> ClaudeCodeWorker:
    return ClaudeCodeWorker(location=str(root))


# ────────── the type:"user" bucket ──────────


class TestUserMessageClassification:
    """One on-disk role (`type:"user"`) → four semantic roles."""

    def test_real_keyboard_input_is_human(self):
        r = _worker(Path("/x")).to_round(cc_msg("u1", "hello", mtype="user"))
        assert (r.role, r.speaker) == ("human", "user")

    def test_tool_result_echo_back_is_tool(self):
        r = _worker(Path("/x")).to_round({
            "type": "user", "uuid": "t1", "toolUseResult": {"ok": True},
            "message": {"content": [{"type": "tool_result", "content": "out"}]},
        })
        assert (r.role, r.speaker) == ("tool", "tool")

    def test_harness_meta_is_system(self):
        r = _worker(Path("/x")).to_round(cc_msg("m1", "caveat", mtype="user", isMeta=True))
        assert (r.role, r.speaker) == ("system", "harness")

    def test_slash_command_artifact_is_system(self):
        r = _worker(Path("/x")).to_round({
            "type": "user", "uuid": "s1",
            "message": {"content": [{"type": "text", "text": "<command-name>/foo"}]},
        })
        assert (r.role, r.speaker) == ("system", "harness")


# ────────── round content ──────────


def test_assistant_text_and_tool_use_are_preserved_typed():
    r = _worker(Path("/x")).to_round({
        "type": "assistant", "uuid": "a1",
        "message": {"content": [
            {"type": "text", "text": "let me look"},
            {"type": "tool_use", "name": "Read", "input": {"file": "x"}},
        ]},
    })
    assert [b.type for b in r.content] == ["text", "tool_use"]
    assert r.content[1].input == '{"file": "x"}'  # dict serialized, kept typed


def test_non_message_rows_are_not_rounds():
    """Summaries and other non-conversation lines are skipped, not errored."""
    assert _worker(Path("/x")).to_round({"type": "summary", "summary": "s"}) is None


# ────────── file → session ──────────


def test_a_jsonl_file_becomes_a_probed_session(tmp_path):
    root = tmp_path / "claude"
    stage_claude_session(root, "proj-x", "sess-uuid", CLAUDE_CHAT)
    probe = _worker(root).probe(str(root / "proj-x" / "sess-uuid.jsonl"))
    assert probe.session_id == "sess-uuid"                 # from filename
    assert probe.created_at == "2026-07-11T00:00:00Z"      # first round's time
    assert probe.metadata["project"] == "proj-x"
    assert len(probe.sha256) == 64


def test_list_sources_walks_nested_project_dirs(tmp_path):
    root = tmp_path / "claude"
    stage_claude_session(root, "alpha", "s1", [cc_msg("u1", "a", mtype="user")])
    stage_claude_session(root, "beta/nested", "s2", [cc_msg("u1", "b", mtype="user")])
    sids = sorted(p.session_id for p in _worker(root).list_sources())
    assert sids == ["s1", "s2"]


# ────────── resumable cursor ──────────


def test_read_after_resumes_from_a_round_id(tmp_path):
    """Given the server's last round, read_after yields only what follows."""
    root = tmp_path / "claude"
    f = stage_claude_session(root, "p", "s", CLAUDE_CHAT)
    worker = _worker(root)

    everything = worker.read_after(str(f), after_round_id=None).rounds
    assert [r.round_id for r in everything] == ["u1", "a1", "t1"]

    resumed = worker.read_after(str(f), after_round_id="a1").rounds
    assert [r.round_id for r in resumed] == ["t1"]


def test_a_stale_offset_hint_is_validated_then_ignored(tmp_path):
    """The cached line offset is only trusted if the line it points at
    actually carries the cursor; otherwise the worker rescans."""
    root = tmp_path / "claude"
    # No summary line, so line 3 (1-indexed) is t1 → hint=3 is honest.
    f = stage_claude_session(root, "p", "s", CLAUDE_CHAT[1:])
    from conftest import append_jsonl
    append_jsonl(f, [cc_msg("a2", "next", mtype="assistant")])
    worker = _worker(root)

    # Honest hint → fast path.
    assert [r.round_id for r in worker.read_after(str(f), "t1", hint_line_offset=3).rounds] == ["a2"]
    # Lying hint (points past the file / at the wrong line) → rescan, same answer.
    assert [r.round_id for r in worker.read_after(str(f), "t1", hint_line_offset=99).rounds] == ["a2"]


# ────────── endpoint namespacing ──────────


def test_same_upstream_id_at_two_locations_does_not_collide():
    """loc-code prefix keeps a shared upstream id distinct per endpoint."""
    a = ClaudeCodeWorker(location="/loc/a")
    b = ClaudeCodeWorker(location="/loc/b")
    assert a.mint_session_id("019e-abcd-e0f4") != b.mint_session_id("019e-abcd-e0f4")
    assert a.mint_session_id("019e-abcd-e0f4").endswith("-e0f4")
