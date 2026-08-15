"""``cb init`` — take over an existing repository from a start point.

The default case is "there is already a git repo with things in it". An empty
repo is the degenerate case. Nothing is rewritten: whatever history exists
becomes the fact layer's history, one ref away. See docs/v2/works/cli.md §2.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import blob, layers as L
from .gitrepo import Git

HOOK_NAMES = ("pre-commit", "commit-msg", "post-commit", "post-checkout", "reference-transaction")

HOOK_TEMPLATE = """#!/usr/bin/env python3
# collectbase hook — 逻辑全在库里,这里只是入口
import sys

try:
    from collectbase.hooks import ENTRIES
except ImportError:
    sys.stderr.write(
        "✗ collectbase 未安装到当前 python3(%s)。\\n"
        "    pip install collectbase\\n"
        "  若装在 venv 里,请在激活 venv 的环境下使用 git。\\n" % sys.executable
    )
    raise SystemExit(1)

raise SystemExit(ENTRIES["{name}"](sys.argv[1:]))
"""


class InitError(RuntimeError):
    pass


@dataclass
class Plan:
    start_point: str
    created: list[str]
    reused: list[str]
    adopted: int


def preflight(git: Git) -> None:
    if not (git.root / ".git").exists() and not git.ok("rev-parse", "--git-dir"):
        raise InitError("这里不是一个 git 仓库")
    if git.text("status", "--porcelain", check=False):
        raise InitError("工作区有未提交的改动,先提交或 stash")
    if git.resolve("HEAD") is not None and git.head_ref() is None:
        raise InitError("HEAD 处于游离状态,先 checkout 一个分支")


def run(git: Git, names: list[str]) -> Plan:
    layers = L.Layers(tuple(names))
    preflight(git)

    if L.read(git) is not None:
        return _reinit(git, layers)

    for ref in layers.managed_refs():
        if git.resolve(ref) is not None:
            raise InitError(f"分支 {ref} 已存在,与将要建立的拓扑冲突")

    # ① 起始点位 = 当前 HEAD(空仓库则从零开始)
    # ② 既有内容全部划归最底层 —— 已经存在的东西就是"给定的",而且这是唯一
    #    能让不变量在 init 当场成立的划分:其余层为空,并集即底层。
    adopted = len(git.ls_tree("HEAD")) if git.resolve("HEAD") else 0

    # ③ 写锚定与 hook,提交到当前分支;这个提交就是起始点位
    _write_anchor(git, layers)
    _ignore_store(git)
    git.run("add", "--", L.ANCHOR, ".gitignore")
    git.run("-c", "core.hooksPath=", "commit", "-q", "-m",
            f"[{layers.bottom}] collectbase: init")
    start = git.resolve("HEAD")
    assert start

    created, reused = [], []
    # ④ layer/<底层> 直接复用起始点位:既有历史原样成为事实层的历史
    git.update_ref(layers.layer_ref(layers.bottom), start, reason="collectbase: init")
    reused.append(layers.layer_ref(layers.bottom))

    # ⑤ 其余各层是空树孤儿提交
    empty = git.empty_tree()
    for name in layers.names[1:]:
        oid = git.commit_tree(empty, [], f"[{layers.bottom}] collectbase: init layer/{name}")
        git.update_ref(layers.layer_ref(name), oid, reason="collectbase: init")
        created.append(layers.layer_ref(name))

    # ⑥ 各条 stack 此刻的并集树 == 底层树,所以直接共享起始点位这一个提交
    for name in layers.stack_names:
        git.update_ref(layers.stack_ref(name), start, reason="collectbase: init")
        reused.append(layers.stack_ref(name))

    git.update_ref(L.PROJECTED_REF, start, reason="collectbase: init")

    # ⑦ 装 hook —— 必须最后做,否则 reference-transaction 会拦住上面的 update-ref
    _write_hooks(git)

    # ⑧ 切到写入面并上锁
    git.run("checkout", "-q", layers.write_face.removeprefix("refs/heads/"))

    return Plan(start, created, reused, adopted)


def _reinit(git: Git, layers: L.Layers) -> Plan:
    """Already initialised — a fresh clone only needs the local config back.

    `core.hooksPath` does not travel with a clone. That is git's safety design;
    it is not something to route around.
    """
    _write_hooks(git)
    face = layers.write_face.removeprefix("refs/heads/")
    if git.resolve(layers.write_face) is not None:
        git.run("checkout", "-q", face)
    start = L.start_point(git) or git.resolve("HEAD") or ""
    return Plan(start, [], list(layers.managed_refs()), 0)


def _write_anchor(git: Git, layers: L.Layers) -> None:
    (git.root / L.ANCHOR).write_text(L.dump(layers))


def _ignore_store(git: Git) -> None:
    """The store lives in the worktree so it can be browsed as a media library,
    which means git must be told to ignore it — otherwise the blobs themselves
    get staged, converted, and end up as symlinks pointing at themselves."""
    p = git.root / ".gitignore"
    lines = p.read_text().splitlines() if p.exists() else []
    if blob.BLOB_DIR + "/" not in lines:
        lines.append(blob.BLOB_DIR + "/")
        p.write_text("\n".join(lines) + "\n")


def _write_hooks(git: Git) -> None:
    d = git.git_dir / L.HOOK_DIR
    d.mkdir(parents=True, exist_ok=True)
    for name in HOOK_NAMES:
        p = d / name
        p.write_text(HOOK_TEMPLATE.format(name=name))
        p.chmod(0o755)
    git.run("config", "core.hooksPath", str(d))


def _unused() -> Path:  # pragma: no cover
    return Path()
