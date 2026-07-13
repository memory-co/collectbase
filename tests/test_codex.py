"""CodexWorker — envelope parsing + synthesized round_id.

Codex stresses two hooks claude-code didn't: the session id lives in an
in-file ``session_meta`` record (filename is the fallback), and there's
no stable per-envelope id so the worker synthesizes a deterministic one.
Scenarios cover the envelope→round mapping, the skip rules that stop
double-counting, and that resume works off the synthesized ids.
"""
from __future__ import annotations

from pathlib import Path

from collectbase.workers.codex import CodexWorker
from conftest import codex_env, write_jsonl

_UUID = "019e5791-ab3d-76b1-8bcc-e0f410415f83"


def _worker(root: Path) -> CodexWorker:
    return CodexWorker(location=str(root))


def _rollout(root: Path, uuid: str = _UUID) -> Path:
    return root / "2026/05/24" / f"rollout-2026-05-24T01-20-24-{uuid}.jsonl"


# ────────── session identity ──────────


class TestSessionIdentity:

    def test_id_comes_from_the_in_file_session_meta(self, tmp_path):
        root = tmp_path / "codex"
        f = _rollout(root)
        write_jsonl(f, [
            codex_env("session_meta", {"id": _UUID, "cwd": "/proj", "cli_version": "0.133.0"}),
            codex_env("event_msg", {"type": "user_message", "message": "hi"}),
        ])
        probe = _worker(root).probe(str(f))
        assert probe.session_id == _UUID
        assert probe.created_at == "2026-05-24T00:00:00Z"
        assert probe.metadata["cwd"] == "/proj"
        assert probe.metadata["cli_version"] == "0.133.0"

    def test_id_falls_back_to_filename_when_meta_missing(self, tmp_path):
        """Truncated file with no session_meta — the uuid still comes off
        the filename so the session isn't lost."""
        root = tmp_path / "codex"
        f = _rollout(root)
        write_jsonl(f, [codex_env("event_msg", {"type": "user_message", "message": "no meta"})])
        assert _worker(root).probe(str(f)).session_id == _UUID


# ────────── envelope → round ──────────


class TestEnvelopeMapping:

    def test_user_and_agent_messages_become_human_and_assistant(self, tmp_path):
        root = tmp_path / "codex"
        f = _rollout(root)
        write_jsonl(f, [
            codex_env("session_meta", {"id": _UUID}),  # not a round
            codex_env("event_msg", {"type": "user_message", "message": "hello"}),
            codex_env("event_msg", {"type": "agent_message", "message": "hi back"}),
        ])
        rounds = _worker(root).read_after(str(f), after_round_id=None).rounds
        assert [r.role for r in rounds] == ["human", "assistant"]
        assert [r.content[0].text for r in rounds] == ["hello", "hi back"]

    def test_function_call_and_output_become_tool_use_and_result(self, tmp_path):
        root = tmp_path / "codex"
        f = _rollout(root)
        write_jsonl(f, [
            codex_env("response_item", {"type": "function_call", "name": "shell", "arguments": "ls"}),
            codex_env("response_item", {"type": "function_call_output", "output": "a.txt\nb.txt"}),
        ])
        rounds = _worker(root).read_after(str(f), after_round_id=None).rounds
        assert [(r.role, r.content[0].type) for r in rounds] == [
            ("assistant", "tool_use"), ("tool", "tool_result")]
        assert rounds[0].content[0].name == "shell"
        assert "a.txt" in rounds[1].content[0].text

    def test_encrypted_reasoning_is_marked_not_dropped(self, tmp_path):
        root = tmp_path / "codex"
        f = _rollout(root)
        write_jsonl(f, [
            codex_env("response_item", {"type": "reasoning", "summary": [], "encrypted_content": "abc=="}),
        ])
        rounds = _worker(root).read_after(str(f), after_round_id=None).rounds
        assert rounds[0].content[0].type == "thinking"
        assert "encrypted" in rounds[0].content[0].thinking.lower()

    def test_telemetry_and_meta_are_not_rounds(self, tmp_path):
        """session_meta / turn_context / telemetry event_msg / the wire-form
        response_item.message (a duplicate of event_msg) are all skipped."""
        root = tmp_path / "codex"
        f = _rollout(root)
        write_jsonl(f, [
            codex_env("session_meta", {"id": _UUID}),
            codex_env("turn_context", {"cwd": "/p"}),
            codex_env("event_msg", {"type": "user_message", "message": "q"}),
            codex_env("response_item", {"type": "message", "role": "user",
                                        "content": [{"type": "input_text", "text": "q"}]}),
            codex_env("event_msg", {"type": "task_started"}),
            codex_env("event_msg", {"type": "token_count", "total": 5}),
            codex_env("event_msg", {"type": "agent_message", "message": "a"}),
        ])
        rounds = _worker(root).read_after(str(f), after_round_id=None).rounds
        assert [r.role for r in rounds] == ["human", "assistant"]


# ────────── synthesized round_id ──────────


class TestSynthesizedRoundId:

    def test_ids_are_deterministic_and_unique_per_record(self, tmp_path):
        """No stable upstream id, so we hash canonical JSON — reproducible
        across restarts (so resume works) and distinct per envelope."""
        worker = _worker(tmp_path)
        records = [
            codex_env("event_msg", {"type": "user_message", "message": "a"}),
            codex_env("event_msg", {"type": "agent_message", "message": "b"}),
        ]
        ids = [worker.round_id(r) for r in records]
        assert all(i.startswith("cx-") for i in ids)
        assert len(set(ids)) == 2
        assert worker.round_id(records[0]) == worker.round_id(dict(records[0]))

    def test_read_after_resumes_from_a_synthesized_cursor(self, tmp_path):
        root = tmp_path / "codex"
        f = _rollout(root)
        write_jsonl(f, [
            codex_env("event_msg", {"type": "user_message", "message": "first"}),
            codex_env("event_msg", {"type": "agent_message", "message": "second"}),
        ])
        worker = _worker(root)
        first = worker.read_after(str(f), after_round_id=None)
        cursor = first.rounds[-1].round_id

        from conftest import append_jsonl
        append_jsonl(f, [codex_env("event_msg", {"type": "agent_message", "message": "third"})])
        resumed = worker.read_after(str(f), after_round_id=cursor,
                                    hint_line_offset=first.next_line_offset).rounds
        assert [r.content[0].text for r in resumed] == ["third"]
