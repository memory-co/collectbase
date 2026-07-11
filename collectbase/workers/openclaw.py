"""Openclaw worker — HTTP-backed session source (polling).

Exercises the ``PollWorker`` tier (docs/worker.md §7): no files on disk,
the engine polls ``list_remote`` every ``poll`` and pulls ``fetch``.
Per-session change detection rides the ETag: the probe's ``sha256`` = the
session's ETag, so the engine short-circuits unchanged sessions exactly
as it does for a file hash.

The HTTP surface is provisional (openclaw's public contract isn't frozen
yet — see the TODOs). It's factored through ``_get_json`` so the wire
shape is one method to adjust, and tests can inject responses without a
network. Expected shapes:

  GET  {location}/sessions
       → {"sessions": [{"id", "etag", "created_at", "cwd"?, "title"?}, …]}
  GET  {location}/sessions/{id}?after={round_id}
       → {"rounds": [{"id", "role", "ts"?, "content": [{"type","text"}, …]}, …]}
"""
from __future__ import annotations

from ..format import Block, Round, Text
from ..worker import PollWorker, Probe, register


def _blocks(raw) -> list:
    """Map openclaw content blocks to standard ones (text → Text, else
    kept verbatim via the Block escape hatch)."""
    out = []
    for b in raw or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") in (None, "text"):
            blk = Text(b.get("text") or "")
        else:
            blk = Block(b["type"], **{k: v for k, v in b.items() if k != "type"})
        if blk is not None:
            out.append(blk)
    return out


@register
class OpenclawWorker(PollWorker):
    source = "openclaw"
    default_location = None  # no sensible default URL — must be configured
    poll = "30s"

    def __init__(self, location=None, label=None, *, auth_key=None, poll=None, **extra):
        super().__init__(location, label, **extra)
        self.auth_key = auth_key
        if poll:
            self.poll = poll

    # ─── HTTP surface (one method to adjust; overridable in tests) ───

    def _get_json(self, path: str, params: dict | None = None) -> dict:
        try:
            import httpx
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("openclaw worker needs httpx — install `collectbase[http]`.") from e
        headers = {"Authorization": f"Bearer {self.auth_key}"} if self.auth_key else {}
        url = f"{self.location.rstrip('/')}{path}"
        resp = httpx.get(url, params=params, headers=headers, timeout=30.0)
        resp.raise_for_status()
        return resp.json()

    # ─── PollWorker contract ───

    def list_remote(self):
        for s in self._get_json("/sessions").get("sessions", []):
            meta = {k: s[k] for k in ("cwd", "title") if s.get(k)}
            yield Probe(
                source_id=str(s["id"]),
                session_id=str(s["id"]),
                sha256=str(s.get("etag") or ""),
                created_at=str(s.get("created_at") or ""),
                metadata=meta,
            )

    def fetch(self, source_id: str, after_round_id):
        params = {"after": after_round_id} if after_round_id else None
        data = self._get_json(f"/sessions/{source_id}", params=params)
        for m in data.get("rounds", []):
            yield Round(
                id=str(m["id"]),
                role=m.get("role"),
                at=m.get("ts"),
                content=_blocks(m.get("content")),
            )
