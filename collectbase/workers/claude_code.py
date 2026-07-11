"""Claude Code worker — reads ``~/.claude/projects/**/*.jsonl``.

Each ``.jsonl`` file is one session; each line is one platform message;
``round_id`` = the message ``uuid``.

The worker body is small on purpose (see docs/worker.md §4): the engine
does watching / hashing / line-seek / cursor / retry. What's left here is
the only genuinely claude-code-specific logic:

  - ``_classify`` — ``type:"user"`` on disk is a 4-way bucket (real human
    input / tool_result echo-back / harness-injected meta / slash-command
    artifacts); disambiguate it.
  - ``_block`` — map each raw content block to a standard ContentBlock.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote

from ..format import Block, Text, Thinking, ToolResult, ToolUse
from ..worker import JsonlWorker, register


def _classify(msg: dict) -> tuple[str, str]:
    """Classify a ``type:"user"`` message into (role, speaker). Judged
    most-stable-signal first: a CLI-level field (``toolUseResult``)
    outlasts API content-shape changes; the block ``type`` is the API
    fallback; text prefixes are last (brittle to harness rebranding)."""
    # 1. tool_result echo-back (CLI-level signal, most stable)
    if "toolUseResult" in msg:
        return ("tool", "tool")
    content = msg.get("message", {}).get("content", [])
    if (
        isinstance(content, list)
        and content
        and isinstance(content[0], dict)
        and content[0].get("type") == "tool_result"
    ):
        return ("tool", "tool")
    # 2. CLI-flagged meta (caveat etc.)
    if msg.get("isMeta"):
        return ("system", "harness")
    # 3. Harness-injected slash-command artifacts — text prefix only.
    text: str | None = None
    if isinstance(content, str):
        text = content
    elif (
        isinstance(content, list)
        and content
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
    ):
        text = content[0].get("text", "")
    if text:
        stripped = text.lstrip()
        for prefix in (
            "<command-name>",
            "<local-command-stdout>",
            "<local-command-caveat>",
            "[Request interrupted by user]",
        ):
            if stripped.startswith(prefix):
                return ("system", "harness")
    # 4. Default — actual human input.
    return ("human", "user")


def _block(raw):
    """One raw content block → a standard ContentBlock (or None to drop)."""
    if not isinstance(raw, dict):
        return None
    t = raw.get("type")
    if t == "text":
        return Text(raw.get("text") or "")
    if t == "thinking":
        return Thinking(raw.get("thinking") or "")
    if t == "tool_use":
        return ToolUse(raw.get("name", "tool"), raw.get("input", ""))
    if t == "tool_result":
        c = raw.get("content", "")
        if isinstance(c, list):
            c = json.dumps(c, ensure_ascii=False)
        return ToolResult(str(c))
    return Block(t or "unknown", **{k: v for k, v in raw.items() if k != "type"})


@register
class ClaudeCodeWorker(JsonlWorker):
    source = "claude-code"
    default_location = str(Path.home() / ".claude" / "projects")
    glob = "**/*.jsonl"

    def round_id(self, rec: dict) -> str | None:
        return rec.get("uuid")

    def to_round(self, rec: dict):
        t = rec.get("type")
        if t not in ("user", "assistant"):
            return None  # summaries / meta rows aren't rounds
        role, speaker = (
            ("assistant", "assistant") if t == "assistant" else _classify(rec)
        )
        raw_content = rec.get("message", {}).get("content", [])
        if isinstance(raw_content, str):
            blocks = [Text(raw_content)]
        else:
            blocks = [_block(b) for b in raw_content]
        blocks = [b for b in blocks if b is not None]
        if not blocks:
            return None
        # Round() lives on the format layer; import lazily to keep this
        # module focused on parsing.
        from ..format import Round

        return Round(
            id=rec.get("uuid", ""),
            role=role,
            speaker=speaker,
            at=rec.get("timestamp"),
            parent=rec.get("parentUuid"),
            cwd=rec.get("cwd"),
            sidechain=bool(rec.get("isSidechain")),
            content=blocks,
        )

    def describe_session(self, path: Path, head: dict | None = None) -> dict:
        meta = {"project": unquote(path.parent.name), "path": str(path)}
        if head and head.get("cwd"):
            meta["cwd"] = head["cwd"]
        return meta
