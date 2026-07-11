"""Engine — drives workers into a sink under a checkpoint cursor.

There is exactly one place where "decide what's new and send it" lives:
``_sync_one_source`` (the 7 steps below). Backfill runs it over every
``worker.list_sources()``; live fs-watch / polling run it over the
touched source. Same code, same logging, same status.

Sync flow (``_sync_one_source``):

  1. worker.probe(source_id)          → sha256 + upstream id + metadata
  2. checkpoint sha matches?          → skip (source unchanged)
  3. sink.ensure_session(...)         → the server's current cursor
  4. worker.read_after(...)           → rounds strictly after that cursor
  5. sink.append_rounds(...)          → append (optimistic concurrency)
  6. on conflict: re-read after the server's actual cursor, retry once
  7. update checkpoint (sha, last_round_id, line_offset)
"""
from __future__ import annotations

import asyncio
import collections
import datetime as _dt
import logging
import time
from pathlib import Path
from typing import Iterable

from .checkpoint import CheckpointStore
from .format import (
    AppendRoundsRequest,
    AppendRoundsResponse,
    EnsureSessionRequest,
    SourceProbe,
)
from .sink import Sink
from .worker import PollWorker, Worker

_log = logging.getLogger("collectbase.engine")

_ISO = lambda: _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds").replace(
    "+00:00", "Z"
)

_COUNTER_KEYS = ("discovered", "imported", "appended", "skipped", "errors", "index_errors")


def _zero() -> dict:
    return {k: 0 for k in _COUNTER_KEYS}


def _parse_interval(spec: str) -> float:
    """"30s" / "5m" / "1h" → seconds. Bare number → seconds."""
    spec = str(spec).strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600}
    if spec and spec[-1] in mult:
        return float(spec[:-1]) * mult[spec[-1]]
    return float(spec)


def _endpoint_key(w: Worker) -> str:
    return f"{w.source}@{w.label or w.location}"


class Engine:
    """Owns the sync loop over a fixed set of workers."""

    def __init__(
        self,
        workers: Iterable[Worker],
        sink: Sink,
        checkpoints: CheckpointStore,
        debounce_ms: int = 200,
    ):
        self.workers: list[Worker] = list(workers)
        self.sink = sink
        self.checkpoints = checkpoints
        self.debounce_seconds = debounce_ms / 1000.0

        self.running = False
        self.phase = "stopped"  # stopped → backfilling → watching
        self._start_ts: float | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[tuple[Worker, Path]] | None = None
        self._worker_task: asyncio.Task | None = None
        self._backfill_task: asyncio.Task | None = None
        self._poll_tasks: list[asyncio.Task] = []
        self._observer = None
        self._pending_at: dict[Path, float] = {}
        self._totals = {"_total": _zero()}
        self._recent: collections.deque[dict] = collections.deque(maxlen=50)

    # ─── public reads ───

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self._start_ts if self._start_ts else 0.0

    def status(self) -> dict:
        return {
            "phase": self.phase,
            "running": self.running,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "workers": [_endpoint_key(w) for w in self.workers],
            "totals": dict(self._totals),
            "recent": list(reversed(list(self._recent)))[:10],
            "checkpoints": None,  # filled in by facade if desired
        }

    # ─── lifecycle ───

    async def start(self) -> dict:
        if self.running:
            return {"status": "already_running", "phase": self.phase}
        self.running = True
        self.phase = "backfilling"
        self._start_ts = time.monotonic()
        self._totals = {"_total": _zero()}
        self._recent.clear()
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()

        # Observer + worker drain FIRST so events during backfill queue up.
        self._observer = _make_observer(self)
        if self._observer is not None:
            self._observer.start()
        self._worker_task = asyncio.create_task(self._worker_loop())
        for w in self.workers:
            if isinstance(w, PollWorker):
                self._poll_tasks.append(asyncio.create_task(self._poll_loop(w)))
        self._backfill_task = asyncio.create_task(self._run_backfill())

        _log.info("engine started workers=%s", [_endpoint_key(w) for w in self.workers])
        return {"status": "started", "phase": "backfilling"}

    async def _run_backfill(self) -> None:
        try:
            for w in self.workers:
                ep = _endpoint_key(w)
                try:
                    for probe in w.list_sources():
                        stats = await self._sync_one_source(w, probe.source_id)
                        self._accumulate(stats, ep)
                except Exception as e:
                    _log.exception("backfill failed endpoint=%s", ep)
                    self._record_error(ep, f"backfill: {e}", ep)
        finally:
            self.phase = "watching"
            _log.info("backfill done phase=watching totals=%s", self._totals["_total"])

    async def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        self.phase = "stopped"
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=1.0)
            except Exception:
                pass
            self._observer = None
        for task in [self._backfill_task, self._worker_task, *self._poll_tasks]:
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._backfill_task = self._worker_task = None
        self._poll_tasks = []
        self._start_ts = None

    # ─── inbound events ───

    def on_event(self, worker: Worker, path: Path) -> None:
        if not self.running or self._loop is None or self._queue is None:
            return
        self._loop.call_soon_threadsafe(self._enqueue, worker, path)

    def _enqueue(self, worker: Worker, path: Path) -> None:
        if self._queue is None:
            return
        self._pending_at[path] = time.monotonic()
        try:
            self._queue.put_nowait((worker, path))
        except asyncio.QueueFull:
            pass

    async def _worker_loop(self) -> None:
        assert self._queue is not None
        while True:
            worker, path = await self._queue.get()
            await self._await_settled(path)
            ep = _endpoint_key(worker)
            try:
                stats = await self._sync_one_source(worker, str(path))
                self._accumulate(stats, ep)
            except Exception as e:
                _log.exception("worker iteration failed endpoint=%s path=%s", ep, path)
                self._record_error(str(path), str(e), ep)

    async def _await_settled(self, path: Path) -> None:
        deadline = self._pending_at.get(path, 0.0) + self.debounce_seconds
        while True:
            now = time.monotonic()
            if now >= deadline:
                self._pending_at.pop(path, None)
                return
            await asyncio.sleep(max(0.01, deadline - now))
            deadline = self._pending_at.get(path, 0.0) + self.debounce_seconds

    async def _poll_loop(self, worker: PollWorker) -> None:
        interval = _parse_interval(worker.poll)
        ep = _endpoint_key(worker)
        while True:
            try:
                for probe in worker.list_sources():
                    stats = await self._sync_one_source(worker, probe.source_id)
                    self._accumulate(stats, ep)
            except Exception as e:
                _log.exception("poll failed endpoint=%s", ep)
                self._record_error(ep, f"poll: {e}", ep)
            await asyncio.sleep(interval)

    # ─── core sync path (shared by backfill + live + poll) ───

    async def _sync_one_source(self, worker: Worker, source_id: str) -> dict:
        stats = _zero()
        ep = _endpoint_key(worker)

        # 1. Probe
        try:
            probe = worker.probe(source_id)
        except Exception as e:
            _log.exception("probe failed endpoint=%s source=%s", ep, source_id)
            self._record_error(source_id, f"probe: {e}", ep)
            stats["errors"] = 1
            return stats
        if probe is None:
            return stats
        stats["discovered"] = 1

        sid = worker.mint_session_id(probe.session_id)

        # 2. Checkpoint short-circuit
        ckpt = await self.checkpoints.get(worker.source, worker.location, probe.session_id)
        if ckpt and ckpt["sha256"] == probe.sha256:
            stats["skipped"] = 1
            return stats

        # 3. Ask the sink where its cursor is
        try:
            ensure = await self.sink.ensure_session(
                EnsureSessionRequest(
                    source=worker.source,
                    session_id=sid,
                    location=worker.location,
                    location_label=worker.label,
                )
            )
        except Exception as e:
            _log.exception("ensure_session failed sid=%s", sid)
            self._record_error(sid, f"ensure: {e}", ep)
            stats["errors"] = 1
            return stats
        server_last = ensure.last_round_id
        hint_offset = ckpt["line_offset"] if ckpt else 0

        # 4. Read incremental rounds
        try:
            batch = worker.read_after(source_id, after_round_id=server_last, hint_line_offset=hint_offset)
        except Exception as e:
            _log.exception("read_after failed sid=%s", sid)
            self._record_error(sid, f"read_after: {e}", ep)
            stats["errors"] = 1
            return stats

        # 5 + 6. Append, retry once on conflict
        if batch.rounds:
            result, used_offset = await self._send_with_conflict_retry(
                worker, probe, sid, batch, expected_prev=server_last
            )
            if result is None:
                stats["errors"] = 1
                return stats
            new_last = result.new_last_round_id
            appended = result.appended_count
        else:
            new_last = server_last
            appended = 0
            used_offset = batch.next_line_offset
            result = None

        # 7. Update checkpoint
        await self.checkpoints.upsert(
            source=worker.source,
            location=worker.location,
            session_id=probe.session_id,
            sha256=probe.sha256,
            last_round_id=new_last,
            line_offset=used_offset,
            updated_at=_ISO(),
        )

        if appended > 0:
            if server_last is None:
                stats["imported"] = 1
                self._record(sid, "imported", ep, rounds=appended)
            else:
                stats["appended"] = 1
                self._record(sid, "rounds_appended", ep, rounds=appended)
            _log.info("ingested endpoint=%s sid=%s appended=%d", ep, sid, appended)
        else:
            stats["skipped"] = 1

        if result is not None and getattr(result, "index_status", "ok") != "ok":
            stats["index_errors"] = 1
            self._record(sid, f"index_{result.index_status}", ep, error=result.index_error)

        return stats

    async def _send_with_conflict_retry(
        self, worker: Worker, probe: SourceProbe, sid: str, batch, expected_prev: str | None
    ) -> tuple[AppendRoundsResponse | None, int]:
        req = AppendRoundsRequest(
            session_id=sid,
            source=worker.source,
            location=worker.location,
            location_label=worker.label,
            expected_prev_round_id=expected_prev,
            rounds=batch.rounds,
            created_at=probe.created_at,
            metadata=probe.metadata,
        )
        result = await self.sink.append_rounds(req)
        if result.status == "ok":
            return result, batch.next_line_offset

        _log.warning(
            "append conflict sid=%s expected=%s actual=%s; retrying once",
            sid, expected_prev, result.actual_last_round_id,
        )
        try:
            retry_batch = worker.read_after(
                probe.source_id, after_round_id=result.actual_last_round_id, hint_line_offset=0
            )
        except Exception:
            _log.exception("re-read after conflict failed sid=%s", sid)
            return None, batch.next_line_offset

        if not retry_batch.rounds:
            return (
                AppendRoundsResponse(
                    status="ok",
                    session_id=sid,
                    new_last_round_id=result.actual_last_round_id,
                    appended_count=0,
                ),
                retry_batch.next_line_offset,
            )

        retry_result = await self.sink.append_rounds(
            AppendRoundsRequest(
                session_id=sid,
                source=worker.source,
                location=worker.location,
                location_label=worker.label,
                expected_prev_round_id=result.actual_last_round_id,
                rounds=retry_batch.rounds,
                created_at=probe.created_at,
                metadata=probe.metadata,
            )
        )
        if retry_result.status == "ok":
            return retry_result, retry_batch.next_line_offset
        _log.error("conflict persists after retry sid=%s", sid)
        self._record_error(sid, "conflict persists", _endpoint_key(worker))
        return None, retry_batch.next_line_offset

    # ─── bookkeeping ───

    def _accumulate(self, delta: dict, endpoint: str) -> None:
        for k, v in delta.items():
            if k not in _COUNTER_KEYS:
                continue
            self._totals["_total"][k] = self._totals["_total"].get(k, 0) + v
            slice_ = self._totals.setdefault(endpoint, _zero())
            slice_[k] = slice_.get(k, 0) + v

    def _record(self, session_id: str, event: str, endpoint: str, **extra) -> None:
        self._recent.append(
            {"at": _ISO(), "session_id": session_id, "event": event, "endpoint": endpoint, **extra}
        )

    def _record_error(self, key, msg: str, endpoint: str) -> None:
        self._recent.append(
            {"at": _ISO(), "session_id": str(key), "event": "error", "error": msg, "endpoint": endpoint}
        )


def _make_observer(engine: Engine):
    """Build a watchdog observer over every file worker's watch roots.
    Returns None if watchdog isn't installed (backfill/poll still work)."""
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers.polling import PollingObserver
    except ImportError:
        _log.info("watchdog not installed; live fs-watch disabled (backfill/poll still run)")
        return None

    class _Handler(FileSystemEventHandler):
        def __init__(self, worker: Worker):
            self.worker = worker

        def on_modified(self, event):
            if not event.is_directory:
                engine.on_event(self.worker, Path(event.src_path))

        def on_created(self, event):
            if not event.is_directory:
                engine.on_event(self.worker, Path(event.src_path))

    observer = PollingObserver(timeout=engine.debounce_seconds)
    scheduled = False
    for worker in engine.workers:
        for root in worker.watch_roots():
            if root.exists():
                observer.schedule(_Handler(worker), str(root), recursive=True)
                scheduled = True
    return observer if scheduled else None
