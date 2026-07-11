from pathlib import Path

from collectbase.workers.codex import CodexWorker
from conftest import append_jsonl, write_jsonl

_UUID = "019e5791-ab3d-76b1-8bcc-e0f410415f83"

CODEX_RECORDS = [
    {"timestamp": "2026-05-24T01:20:24Z", "type": "session_meta",
     "payload": {"id": _UUID, "cwd": "/proj", "cli_version": "0.133.0"}},
    {"timestamp": "2026-05-24T01:20:25Z", "type": "turn_context", "payload": {"cwd": "/proj"}},
    {"timestamp": "2026-05-24T01:20:26Z", "type": "event_msg",
     "payload": {"type": "user_message", "message": "list files"}},
    {"timestamp": "2026-05-24T01:20:27Z", "type": "event_msg",
     "payload": {"type": "agent_message", "message": "sure"}},
    {"timestamp": "2026-05-24T01:20:28Z", "type": "response_item",
     "payload": {"type": "function_call", "name": "shell", "arguments": "ls"}},
    {"timestamp": "2026-05-24T01:20:29Z", "type": "response_item",
     "payload": {"type": "function_call_output", "output": "a.txt\nb.txt"}},
    {"timestamp": "2026-05-24T01:20:30Z", "type": "event_msg",
     "payload": {"type": "token_count", "total": 42}},  # telemetry → skip
]


def _file(tmp_path: Path) -> Path:
    return tmp_path / "sessions" / "2026" / "05" / "24" / f"rollout-2026-05-24T01-20-24-{_UUID}.jsonl"


def _worker(tmp_path: Path) -> CodexWorker:
    return CodexWorker(location=str(tmp_path / "sessions"))


def test_session_id_from_in_file_meta(tmp_path):
    w = _worker(tmp_path)
    f = _file(tmp_path)
    write_jsonl(f, CODEX_RECORDS)
    p = w.probe(str(f))
    assert p.session_id == _UUID
    assert p.created_at == "2026-05-24T01:20:24Z"
    assert p.metadata["cwd"] == "/proj"
    assert p.metadata["cli_version"] == "0.133.0"


def test_session_id_falls_back_to_filename(tmp_path):
    w = _worker(tmp_path)
    f = _file(tmp_path)
    # No session_meta line (truncated file) → id from the filename.
    write_jsonl(f, CODEX_RECORDS[2:])
    assert w.probe(str(f)).session_id == _UUID


def test_envelope_routing_and_skips(tmp_path):
    w = _worker(tmp_path)
    f = _file(tmp_path)
    write_jsonl(f, CODEX_RECORDS)
    rounds = w.read_after(str(f), after_round_id=None).rounds
    assert [(r.role, r.content[0].type) for r in rounds] == [
        ("human", "text"),
        ("assistant", "text"),
        ("assistant", "tool_use"),
        ("tool", "tool_result"),
    ]
    # session_meta / turn_context / token_count are not rounds.
    assert len(rounds) == 4


def test_round_id_synthesized_and_stable(tmp_path):
    w = _worker(tmp_path)
    ids = [w.round_id(r) for r in CODEX_RECORDS]
    assert all(i.startswith("cx-") for i in ids)
    assert len(set(ids)) == len(ids)  # unique per record
    # Deterministic: same record → same id.
    assert w.round_id(CODEX_RECORDS[2]) == w.round_id(dict(CODEX_RECORDS[2]))


def test_incremental_read_after_synthesized_cursor(tmp_path):
    w = _worker(tmp_path)
    f = _file(tmp_path)
    write_jsonl(f, CODEX_RECORDS)
    first = w.read_after(str(f), after_round_id=None)
    cursor = first.rounds[-1].round_id  # last kept round's synthesized id

    append_jsonl(f, [{"timestamp": "2026-05-24T01:21:00Z", "type": "event_msg",
                      "payload": {"type": "agent_message", "message": "done"}}])
    inc = w.read_after(str(f), after_round_id=cursor, hint_line_offset=first.next_line_offset)
    assert [r.content[0].text for r in inc.rounds] == ["done"]
