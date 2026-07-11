"""collectbase — the ingestion boundary.

Collect raw sessions from many sources, normalize each into the standard
session format, and push them into a memory system. See DESIGN.md and
docs/.

Typical use:

    from collectbase import Collectbase, HttpSink
    from collectbase.workers import ClaudeCodeWorker

    cb = await Collectbase.open(
        checkpoint_dir="./collect",
        sink=HttpSink("http://memory-host:8000", api_key="…"),
        workers=[ClaudeCodeWorker()],
    )
    await cb.start()
    ...
    await cb.close()
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .checkpoint import CheckpointStore
from .engine import Engine
from .format import (
    Block,
    Code,
    ContentBlock,
    Probe,
    Round,
    RoundInput,
    SourceProbe,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
)
from .sink import HttpSink, InProcessSink, Sink
from .worker import FileWorker, JsonlWorker, PollWorker, Worker, register

__all__ = [
    "Collectbase",
    # format / builders
    "Round",
    "Text",
    "Thinking",
    "Code",
    "ToolUse",
    "ToolResult",
    "Block",
    "ContentBlock",
    "RoundInput",
    "Probe",
    "SourceProbe",
    # worker bases
    "Worker",
    "FileWorker",
    "JsonlWorker",
    "PollWorker",
    "register",
    # sink
    "Sink",
    "HttpSink",
    "InProcessSink",
    # internals
    "Engine",
    "CheckpointStore",
]


class Collectbase:
    """Facade tying an engine, a sink, and a checkpoint store together."""

    def __init__(self, engine: Engine, checkpoints: CheckpointStore):
        self.engine = engine
        self.checkpoints = checkpoints

    @classmethod
    async def open(
        cls,
        checkpoint_dir: str | Path,
        sink: Sink,
        workers: Iterable[Worker],
        *,
        debounce_ms: int = 200,
    ) -> "Collectbase":
        db_path = Path(checkpoint_dir).expanduser() / "sync.db"
        checkpoints = await CheckpointStore.open(db_path)
        engine = Engine(workers, sink, checkpoints, debounce_ms=debounce_ms)
        return cls(engine, checkpoints)

    async def start(self) -> dict:
        return await self.engine.start()

    async def stop(self) -> None:
        await self.engine.stop()

    async def close(self) -> None:
        await self.engine.stop()
        await self.checkpoints.close()
        # Close the sink's transport if it owns one (e.g. HttpSink's
        # httpx client). Sinks without an aclose (InProcessSink) are no-ops.
        aclose = getattr(self.engine.sink, "aclose", None)
        if aclose is not None:
            await aclose()

    async def status(self) -> dict:
        s = self.engine.status()
        s["checkpoints"] = await self.checkpoints.count()
        return s
