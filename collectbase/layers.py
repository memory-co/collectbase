"""The ``layers`` anchor — the single source of truth.

Everything derives from this one file: which layers exist, their order, the
branch names, the write face, the legal commit tags, and the start point
(the commit that first introduced it). See docs/v2/DESIGN.md §7.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

from .gitrepo import Git, GitError

ANCHOR = "layers"
CONFIG = ".collectbase/config.yaml"
# Hooks live inside .git, not in the tree. Tracking them looks appealing
# ("they travel with the repo"), but checking out a derived branch — whose tree
# is empty — deletes them from the worktree and silently disables every guard.
# And `core.hooksPath` is local config that never travels anyway, so `cb init`
# has to run in each clone regardless; it may as well write the hooks then.
HOOK_DIR = "cb-hooks"
PROJECTED_REF = "refs/collectbase/projected"

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# collectbase's own files. They live in the bottom layer so the agent cannot
# change the topology, but they are mechanism rather than evidence, so the
# read-only bit does not apply — otherwise nobody could ever add a layer.
META_PATHS = frozenset({ANCHOR, ".gitignore", CONFIG})


class LayersError(RuntimeError):
    pass


@dataclass(frozen=True)
class Layers:
    """An ordered stack of layer names, bottom first."""

    names: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.names) < 2:
            raise LayersError("至少要两层(事实层 + 至少一个上层)")
        seen: set[str] = set()
        for n in self.names:
            if not NAME_RE.match(n):
                raise LayersError(f"层名 {n!r} 不合法,只允许 [a-z][a-z0-9_-]*")
            if n in seen:
                raise LayersError(f"层名 {n!r} 重复")
            seen.add(n)

    # ------------------------------------------------------------------ 基本

    @property
    def bottom(self) -> str:
        return self.names[0]

    @property
    def top(self) -> str:
        return self.names[-1]

    def index(self, name: str) -> int:
        try:
            return self.names.index(name)
        except ValueError:
            raise LayersError(f"未知的层 {name!r}") from None

    def __contains__(self, name: object) -> bool:
        return name in self.names

    def __iter__(self):
        return iter(self.names)

    # ------------------------------------------------------------------ 分支

    def layer_ref(self, name: str) -> str:
        return f"refs/heads/layer/{name}"

    def stack_ref(self, name: str) -> str:
        return f"refs/heads/stack/{name}"

    @property
    def write_face(self) -> str:
        return self.stack_ref(self.top)

    @property
    def stack_names(self) -> tuple[str, ...]:
        """Stacks that actually get a branch — the bottom needs none, since
        ``stack/<bottom>`` would be identical to ``layer/<bottom>``."""
        return self.names[1:]

    def prefix(self, name: str) -> tuple[str, ...]:
        """Layers from the bottom up to and including ``name``."""
        return self.names[: self.index(name) + 1]

    def stacks_containing(self, layer: str) -> tuple[str, ...]:
        """Stack branches that must be recomputed when ``layer`` changes,
        excluding the write face (it is where the commit already landed)."""
        i = self.index(layer)
        return tuple(n for n in self.names[max(i, 1) : -1])

    def managed_refs(self) -> tuple[str, ...]:
        return tuple(self.layer_ref(n) for n in self.names) + tuple(
            self.stack_ref(n) for n in self.stack_names
        )


TAG_RE = re.compile(r"^\[([a-z][a-z0-9_-]*)\]")


def tag_of(subject: str) -> str | None:
    """The ``[layer]`` prefix of a commit subject, if present."""
    m = TAG_RE.match(subject.strip())
    return m.group(1) if m else None


def parse(text: str) -> Layers:
    data = yaml.safe_load(text)
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise LayersError(f"{ANCHOR} 必须是一个字符串列表(YAML),自下而上")
    return Layers(tuple(data))


def dump(layers: Layers) -> str:
    head = "# collectbase — 顺序即层序,第一项是事实层\n"
    return head + "".join(f"- {n}\n" for n in layers.names)


def read_at(git: Git, rev: str) -> Layers | None:
    """Read the anchor as of ``rev``. None when the repo is not (yet) managed."""
    try:
        blob = git.run("cat-file", "blob", f"{rev}:{ANCHOR}")
    except GitError:
        return None
    try:
        return parse(blob.decode())
    except LayersError:
        return None


def read(git: Git) -> Layers | None:
    """Read the anchor from the write face if we can find it, else from HEAD.

    Bootstrapping is not circular: every layer branch and every stack contains
    the anchor, because it lives in the bottom layer.
    """
    for ref in _candidate_refs(git):
        got = read_at(git, ref)
        if got is not None:
            return got
    return None


def _candidate_refs(git: Git):
    # HEAD first: when you are standing on the write face, that is the newest
    # anchor there is — including a layer you just added but have not projected
    # yet. Everything else is a fallback for when HEAD is elsewhere.
    yield "HEAD"
    for ref in git.branches("refs/heads/stack/**"):
        yield ref
    for ref in git.branches("refs/heads/layer/**"):
        yield ref


def start_point(git: Git, ref: str | None = None) -> str | None:
    """The commit that first introduced the anchor. Rules apply from here on."""
    ref = ref or "--all"
    out = git.text(
        "log", "--diff-filter=A", "--format=%H", "--reverse", ref, "--", ANCHOR, check=False
    )
    lines = [x for x in out.splitlines() if x]
    return lines[0] if lines else None
