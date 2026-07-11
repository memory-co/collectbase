"""Worker — a data-source adapter.

Three tiers, pick the cheapest that fits (see docs/worker.md §1):

  - ``JsonlWorker`` — append-only line-delimited logs (claude-code,
    codex, most agent tools). Author writes ``to_round`` + ``round_id``;
    the engine does incremental line-seek, hash-skip, cursor bookkeeping.
  - ``FileWorker``  — whole-file-rewrite sources (a JSON file, a SQLite
    export). Author writes ``parse(path) -> Iterable[Round]``.
  - ``Worker``      — the raw 4-method port for non-file / exotic sources
    (HTTP, webhook, DB). ``PollWorker`` is a convenience over it.

Everything else — watching, backfill, hashing, cursor, optimistic-
concurrency retry, checkpoint, session_id minting, error isolation —
belongs to the engine. Workers are stateless ports.
"""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterable, Iterator

from .format import ReadAfterResult, RoundInput, SourceProbe

# Re-export so worker modules can ``from collectbase.worker import Probe``.
Probe = SourceProbe


# ─── registry ─────────────────────────────────────────────────────────

WORKERS: dict[str, type["Worker"]] = {}


def register(cls: type["Worker"]) -> type["Worker"]:
    """Class decorator — make a worker discoverable by ``source`` name."""
    if not getattr(cls, "source", None):
        raise ValueError(f"{cls.__name__} must set a `source` class attribute")
    WORKERS[cls.source] = cls
    return cls


# ─── raw port ─────────────────────────────────────────────────────────


class Worker(ABC):
    """The raw adapter port. One instance = one upstream source AT one
    location (the same source *type* can run against several locations).

    Most authors never subclass this directly — they subclass
    ``JsonlWorker`` / ``FileWorker``. Subclass it for HTTP / exotic
    sources, or use ``PollWorker``.
    """

    source: str = ""  # unique source name; set by subclass
    default_location: str | None = None  # used when config omits location

    def __init__(self, location: str | None = None, label: str | None = None, **extra: Any):
        loc = location if location is not None else self.default_location
        if loc is None:
            raise ValueError(
                f"worker {self.source!r} has no default_location; supply one"
            )
        self.location = loc
        self.label = label or loc
        self.extras = extra

    # ─── identity helpers (don't override) ───

    @property
    def endpoint_id(self) -> str:
        """Stable ``"<source>#<location>"`` key — hash input for id
        minting and the audit key in events / status."""
        return f"{self.source}#{self.location}"

    @property
    def loc_code(self) -> str:
        """8-hex derived from ``endpoint_id``; stable across machines."""
        return hashlib.sha256(self.endpoint_id.encode()).hexdigest()[:8]

    def mint_session_id(self, upstream_id: str) -> str:
        """Canonical id ``sess-<loc8>-<lastseg>`` — the only shape the
        memory side recognizes. ``loc8`` namespaces per (source,
        location) so the same upstream id at two endpoints can't collide.
        """
        if "-" in upstream_id:
            last = upstream_id.rsplit("-", 1)[1] or upstream_id.rstrip("-")
        else:
            last = upstream_id
        return f"sess-{self.loc_code}-{last}"

    # ─── contract ───

    @abstractmethod
    def watch_roots(self) -> list[Path]:
        """Filesystem dirs the engine should fs-watch. ``[]`` for remote."""

    @abstractmethod
    def list_sources(self) -> Iterator[SourceProbe]:
        """Enumerate every upstream session known right now (backfill)."""

    @abstractmethod
    def probe(self, source_id: str) -> SourceProbe | None:
        """Cheap inspection of one source. ``None`` if gone / unrecognized."""

    @abstractmethod
    def read_after(
        self,
        source_id: str,
        after_round_id: str | None,
        hint_line_offset: int = 0,
    ) -> ReadAfterResult:
        """Rounds strictly after ``after_round_id`` (None = from the top).
        ``hint_line_offset`` is the engine's cached seek hint — validate
        before trusting, fall back to a full scan on mismatch."""


# ─── file-backed base ─────────────────────────────────────────────────


class FileWorker(Worker):
    """Base for file-backed sources. Author declares ``glob`` and writes
    ``parse(path) -> Iterable[Round]``; the framework handles globbing,
    watching, hashing, and slicing "rounds after the cursor".

    Override ``session_id`` / ``describe_session`` to customize how a
    path maps to a session and what metadata rides along.
    """

    glob: str | list[str] = "**/*"  # which files under `location` are sessions
    ignore: list[str] = []  # glob patterns to skip

    @property
    def root(self) -> Path:
        return Path(self.location).expanduser()

    # ─── author hooks ───

    def parse(self, path: Path) -> Iterable[RoundInput]:  # pragma: no cover
        """Return all rounds in this file, in order. Required for a plain
        FileWorker; JsonlWorker provides it via ``to_round`` per line."""
        raise NotImplementedError

    def session_id(self, path: Path, head: dict | None = None) -> str:
        """Upstream raw id for this file. Default: filename stem."""
        return path.stem

    def describe_session(self, path: Path, head: dict | None = None) -> dict:
        """Session-level metadata (project / path / …). Default: empty."""
        return {}

    # ─── contract impl ───

    def _globs(self) -> list[str]:
        return [self.glob] if isinstance(self.glob, str) else list(self.glob)

    def _ignored(self, path: Path) -> bool:
        return any(path.match(pat) for pat in self.ignore)

    def watch_roots(self) -> list[Path]:
        return [self.root]

    def _iter_paths(self) -> Iterator[Path]:
        root = self.root
        if not root.exists():
            return
        seen: set[Path] = set()
        for pattern in self._globs():
            for path in sorted(root.glob(pattern)):
                if path in seen or not path.is_file() or self._ignored(path):
                    continue
                seen.add(path)
                yield path

    def list_sources(self) -> Iterator[SourceProbe]:
        for path in self._iter_paths():
            probe = self.probe(str(path))
            if probe is not None:
                yield probe

    def _read_head(self, path: Path) -> dict | None:
        """First parseable record — subclasses that have a notion of
        'record' (JsonlWorker) override; base returns None."""
        return None

    def _created_at(self, path: Path, head: dict | None) -> str:
        """created_at for the probe. Base: empty (server falls back to
        the ingest clock / first round timestamp)."""
        return ""

    def probe(self, source_id: str) -> SourceProbe | None:
        path = Path(source_id)
        try:
            raw = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            return None
        if not raw:
            return None
        head = self._read_head(path)
        return SourceProbe(
            source_id=str(path),
            session_id=self.session_id(path, head),
            sha256=hashlib.sha256(raw).hexdigest(),
            created_at=self._created_at(path, head),
            metadata=self.describe_session(path, head),
        )

    def read_after(
        self,
        source_id: str,
        after_round_id: str | None,
        hint_line_offset: int = 0,
    ) -> ReadAfterResult:
        path = Path(source_id)
        try:
            rounds = list(self.parse(path))
        except FileNotFoundError:
            return ReadAfterResult(rounds=[], next_line_offset=0)
        if after_round_id is None:
            return ReadAfterResult(rounds=rounds, next_line_offset=0)
        for i, r in enumerate(rounds):
            if r.round_id == after_round_id:
                return ReadAfterResult(rounds=rounds[i + 1 :], next_line_offset=0)
        # Cursor not found — treat as fresh; the engine's conflict-retry
        # reconciles against the server's actual cursor.
        return ReadAfterResult(rounds=rounds, next_line_offset=0)


# ─── append-only jsonl base ───────────────────────────────────────────


class JsonlWorker(FileWorker):
    """Base for append-only line-delimited-JSON logs. Author writes
    ``to_round`` (one record → one Round, or None to skip) and
    ``round_id`` (which field is the id). The framework seeks to the
    cached line offset, validates it, and parses only new lines.
    """

    glob: str | list[str] = "**/*.jsonl"

    # ─── author hooks ───

    def to_round(self, record: dict) -> RoundInput | None:  # pragma: no cover
        """One decoded record → a Round, or None to skip this record."""
        raise NotImplementedError

    def round_id(self, record: dict) -> str | None:
        """Which field is the round_id. Default tries common keys;
        override to pick a field or synthesize a deterministic id."""
        return record.get("round_id") or record.get("uuid") or record.get("id")

    # ─── shared plumbing ───

    def parse(self, path: Path) -> Iterator[RoundInput]:
        """Whole-file view (used if someone calls FileWorker.read_after
        on a JsonlWorker); the fast path is ``read_after`` below."""
        for record in self._iter_records(path):
            r = self.to_round(record)
            if r is not None:
                yield r

    def _iter_records(self, path: Path) -> Iterator[dict]:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

    def _read_head(self, path: Path) -> dict | None:
        for record in self._iter_records(path):
            return record
        return None

    def _created_at(self, path: Path, head: dict | None) -> str:
        # First round's timestamp, if any — cheap bounded scan.
        for record in self._iter_records(path):
            r = self.to_round(record)
            if r is not None and r.timestamp:
                return r.timestamp
        return ""

    def _line_round_id(self, line: str) -> str | None:
        line = line.strip()
        if not line:
            return None
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return None
        return self.round_id(record)

    def _locate_start(
        self, lines: list[str], after_round_id: str | None, hint_line_offset: int
    ) -> int:
        """Index of the first line to yield (strictly after the line
        carrying ``after_round_id``). Trust the hint only if it checks
        out; otherwise scan; if the marker is absent, start at 0."""
        if after_round_id is None:
            return 0
        if 0 < hint_line_offset <= len(lines):
            if self._line_round_id(lines[hint_line_offset - 1]) == after_round_id:
                return hint_line_offset
        for i, line in enumerate(lines):
            if self._line_round_id(line) == after_round_id:
                return i + 1
        return 0

    def read_after(
        self,
        source_id: str,
        after_round_id: str | None,
        hint_line_offset: int = 0,
    ) -> ReadAfterResult:
        path = Path(source_id)
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ReadAfterResult(rounds=[], next_line_offset=0)
        lines = raw.splitlines()
        start = self._locate_start(lines, after_round_id, hint_line_offset)
        rounds: list[RoundInput] = []
        for i in range(start, len(lines)):
            line = lines[i].strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            r = self.to_round(record)
            if r is not None:
                rounds.append(r)
        return ReadAfterResult(rounds=rounds, next_line_offset=len(lines))


# ─── remote polling base ──────────────────────────────────────────────


class PollWorker(Worker):
    """Base for non-filesystem sources driven by periodic polling. Author
    writes ``list_remote`` (enumerate sessions + change-tokens) and
    ``fetch`` (rounds after a cursor). The engine polls every ``poll``.
    """

    poll: str = "60s"  # engine polling interval (e.g. "30s", "5m")

    def list_remote(self) -> Iterable[SourceProbe]:  # pragma: no cover
        raise NotImplementedError

    def fetch(
        self, source_id: str, after_round_id: str | None
    ) -> Iterable[RoundInput]:  # pragma: no cover
        raise NotImplementedError

    def watch_roots(self) -> list[Path]:
        return []

    def list_sources(self) -> Iterator[SourceProbe]:
        yield from self.list_remote()

    def probe(self, source_id: str) -> SourceProbe | None:
        for p in self.list_remote():
            if p.source_id == source_id:
                return p
        return None

    def read_after(
        self,
        source_id: str,
        after_round_id: str | None,
        hint_line_offset: int = 0,
    ) -> ReadAfterResult:
        rounds = list(self.fetch(source_id, after_round_id))
        return ReadAfterResult(rounds=rounds, next_line_offset=0)
