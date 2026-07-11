"""Sink — the only contact surface with the memory system.

Two operations, cursor-based and append-only (see docs/DESIGN.md §6):

  ensure_session(req)  → where does the server's cursor stand?
  append_rounds(req)   → write rounds strictly after expected_prev_round_id
                          (status="conflict" if the cursor moved)

The engine depends only on the ``Sink`` protocol. Concrete impls:

  - ``HttpSink``      — POST to a remote memory's /v3/sessions/ensure & /append
  - ``InProcessSink`` — adapt any object exposing the two coroutines
                        (e.g. an embedded IngestService, or a test fake)

Conflict *retry* is the engine's job, not the sink's: the sink reports
``conflict`` + the server's actual cursor faithfully and returns.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .format import (
    AppendRoundsRequest,
    AppendRoundsResponse,
    EnsureSessionRequest,
    EnsureSessionResponse,
)


@runtime_checkable
class Sink(Protocol):
    """Where normalized sessions go. Swapping transports must not change
    this contract (same discipline as seekbase's embed/server forms)."""

    async def ensure_session(self, req: EnsureSessionRequest) -> EnsureSessionResponse: ...

    async def append_rounds(self, req: AppendRoundsRequest) -> AppendRoundsResponse: ...


class InProcessSink:
    """Adapt any object with ``ensure_session`` / ``append_rounds``
    coroutines into a ``Sink``. Used for same-process embedding (wrap a
    live IngestService) and for tests (wrap a fake)."""

    def __init__(self, ingest: Any):
        self._ingest = ingest

    async def ensure_session(self, req: EnsureSessionRequest) -> EnsureSessionResponse:
        return await self._ingest.ensure_session(req)

    async def append_rounds(self, req: AppendRoundsRequest) -> AppendRoundsResponse:
        return await self._ingest.append_rounds(req)


class HttpSink:
    """Push to a remote memory system over HTTP.

    Speaks the existing ingest contract: ``POST {base_url}/v3/sessions/
    ensure`` and ``.../append``. Optional bearer token for the cloud
    form. Requires the ``[http]`` extra (httpx)."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        prefix: str = "/v3/sessions",
        timeout: float = 30.0,
    ):
        try:
            import httpx
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "HttpSink needs httpx — install `collectbase[http]`."
            ) from e
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._prefix = prefix.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout
        )

    async def ensure_session(self, req: EnsureSessionRequest) -> EnsureSessionResponse:
        resp = await self._client.post(
            f"{self._prefix}/ensure", json=req.model_dump()
        )
        resp.raise_for_status()
        return EnsureSessionResponse.model_validate(resp.json())

    async def append_rounds(self, req: AppendRoundsRequest) -> AppendRoundsResponse:
        resp = await self._client.post(
            f"{self._prefix}/append", json=req.model_dump()
        )
        resp.raise_for_status()
        return AppendRoundsResponse.model_validate(resp.json())

    async def aclose(self) -> None:
        await self._client.aclose()
