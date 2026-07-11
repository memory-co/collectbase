"""collectbase CLI.

    collectbase serve  --config collectbase.toml    # run engine (backfill + watch)
    collectbase status --config collectbase.toml    # one-shot backfill, print totals, exit
    collectbase version

Config (TOML):

    checkpoint_dir = "./collect"

    [sink]
    type = "http"                       # only http is usable from the CLI
    base_url = "http://localhost:8000"
    api_key = "…"                       # optional

    [[workers]]
    source = "claude-code"              # location omitted → worker default

    [[workers]]
    source = "codex"
    location = "~/.codex/sessions"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import tomllib
from pathlib import Path

from . import Collectbase
from .sink import HttpSink
from .worker import WORKERS

# Importing the workers package registers the built-in workers.
from . import workers as _builtin_workers  # noqa: F401


def _load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _build_sink(cfg: dict):
    sink_cfg = cfg.get("sink", {})
    stype = sink_cfg.get("type", "http")
    if stype != "http":
        raise SystemExit(
            f"sink.type={stype!r} is not usable from the CLI (only 'http'); "
            "embed collectbase as a library for InProcessSink."
        )
    base_url = sink_cfg.get("base_url")
    if not base_url:
        raise SystemExit("sink.base_url is required for the http sink")
    return HttpSink(base_url, api_key=sink_cfg.get("api_key"))


def _build_workers(cfg: dict) -> list:
    out = []
    for w in cfg.get("workers", []):
        source = w.get("source")
        cls = WORKERS.get(source)
        if cls is None:
            raise SystemExit(
                f"unknown worker source {source!r}; known: {sorted(WORKERS)}"
            )
        extras = {k: v for k, v in w.items() if k not in ("source", "location", "label")}
        out.append(cls(location=w.get("location"), label=w.get("label"), **extras))
    if not out:
        raise SystemExit("config has no [[workers]]")
    return out


async def _serve(cfg: dict) -> None:
    cb = await Collectbase.open(
        checkpoint_dir=cfg.get("checkpoint_dir", "./collect"),
        sink=_build_sink(cfg),
        workers=_build_workers(cfg),
    )
    await cb.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover (Windows)
            pass
    print("collectbase serving; Ctrl-C to stop", file=sys.stderr)
    await stop.wait()
    await cb.close()


async def _status(cfg: dict) -> None:
    """Run a single backfill pass to steady state, print totals, exit."""
    cb = await Collectbase.open(
        checkpoint_dir=cfg.get("checkpoint_dir", "./collect"),
        sink=_build_sink(cfg),
        workers=_build_workers(cfg),
    )
    await cb.start()
    while cb.engine.phase == "backfilling":
        await asyncio.sleep(0.05)
    print(json.dumps(await cb.status(), indent=2, ensure_ascii=False))
    await cb.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="collectbase")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "status"):
        p = sub.add_parser(name)
        p.add_argument("--config", "-c", required=True)
    sub.add_parser("version")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.cmd == "version":
        from importlib.metadata import version

        try:
            print(version("collectbase"))
        except Exception:
            print("0.1.0")
        return 0

    cfg = _load_config(args.config)
    if args.cmd == "serve":
        asyncio.run(_serve(cfg))
    elif args.cmd == "status":
        asyncio.run(_status(cfg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
