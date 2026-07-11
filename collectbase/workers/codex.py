"""Codex CLI worker — reads ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``.

One JSONL file per session, date-partitioned. Each line is an *envelope*
``{"timestamp", "type", "payload"}``. This worker exercises two hooks
that claude-code didn't need (see docs/worker.md §3):

  - ``session_id`` — the id lives in the in-file ``session_meta`` record;
    the filename is only a fallback for truncated files.
  - ``round_id``   — codex puts no stable id on envelopes, so we
    **synthesize** a deterministic one from the record's canonical JSON.
    The engine's cursor matching only needs it to be stable per record
    content, which it is (envelopes differ at least by timestamp).

Round mapping picks ONE surface per kind of content to avoid double-
counting (event_msg/*_message vs response_item/message are two views of
the same generation — we keep the cleaner event_msg text form):

  event_msg/user_message              → human    (text)
  event_msg/agent_message             → assistant (text)
  response_item/function_call         → assistant (tool_use)
  response_item/function_call_output  → tool      (tool_result)
  response_item/reasoning             → assistant (thinking)

Everything else (session_meta, turn_context, telemetry event_msg,
response_item/message) is not a round and is skipped.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..format import Round, Text, Thinking, ToolResult, ToolUse
from ..worker import JsonlWorker, register

# ``rollout-<ISO>-<uuid>.jsonl`` — filename fallback for the session id.
_FILENAME_UUID_RE = re.compile(r"rollout-[\d-]+T[\d-]+-([0-9a-f-]{36})\.jsonl$")


def _canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _uuid_from_filename(path: Path) -> str | None:
    m = _FILENAME_UUID_RE.search(path.name)
    return m.group(1) if m else None


@register
class CodexWorker(JsonlWorker):
    source = "codex"
    default_location = str(Path.home() / ".codex" / "sessions")
    glob = "**/rollout-*.jsonl"

    # ─── synthesized, deterministic round id ───

    def round_id(self, rec: dict) -> str:
        return "cx-" + hashlib.sha256(_canonical(rec).encode("utf-8")).hexdigest()[:16]

    # ─── session identity: in-file meta, filename fallback ───

    def _session_meta(self, path: Path) -> dict | None:
        for rec in self._iter_records(path):
            if rec.get("type") == "session_meta":
                return rec.get("payload") or {}
        return None

    def session_id(self, path: Path, head: dict | None = None) -> str:
        meta = self._session_meta(path)
        if meta and meta.get("id"):
            return meta["id"]
        return _uuid_from_filename(path) or path.stem

    def _created_at(self, path: Path, head: dict | None) -> str:
        meta = self._session_meta(path)
        if meta and meta.get("timestamp"):
            return meta["timestamp"]
        for rec in self._iter_records(path):
            if rec.get("timestamp"):
                return rec["timestamp"]
        return ""

    def describe_session(self, path: Path, head: dict | None = None) -> dict:
        meta = self._session_meta(path) or {}
        out: dict = {"path": str(path)}
        if meta.get("cwd"):
            out["cwd"] = meta["cwd"]
        for k in ("cli_version", "originator", "model_provider"):
            if meta.get(k):
                out[k] = meta[k]
        return out

    # ─── envelope → round ───

    def to_round(self, rec: dict):
        t = rec.get("type")
        payload = rec.get("payload") or {}
        pt = payload.get("type")
        ts = rec.get("timestamp")
        rid = self.round_id(rec)

        if t in ("session_meta", "turn_context"):
            return None
        if t == "event_msg" and pt in ("task_started", "task_complete", "turn_aborted", "token_count"):
            return None
        if t == "response_item" and pt == "message":
            return None  # duplicates the event_msg surface

        if t == "event_msg" and pt == "user_message":
            return Round(id=rid, role="human", speaker="user", at=ts, content=payload.get("message") or "")
        if t == "event_msg" and pt == "agent_message":
            return Round(id=rid, role="assistant", at=ts, content=payload.get("message") or "")
        if t == "response_item" and pt == "function_call":
            return Round(
                id=rid, role="assistant", at=ts,
                content=[ToolUse(payload.get("name") or "function", payload.get("arguments") or "")],
            )
        if t == "response_item" and pt == "function_call_output":
            output = payload.get("output") or ""
            return Round(id=rid, role="tool", speaker="tool", at=ts, content=[ToolResult(output)])
        if t == "response_item" and pt == "reasoning":
            summary = payload.get("summary") or []
            parts = [s["text"] for s in summary if isinstance(s, dict) and s.get("text")]
            if not parts and payload.get("encrypted_content"):
                thinking = "[encrypted reasoning]"
            else:
                thinking = "\n\n".join(parts) or "[empty reasoning]"
            return Round(id=rid, role="assistant", at=ts, content=[Thinking(thinking)])

        # Unknown envelope — skip safely; new codex versions may add types.
        return None
