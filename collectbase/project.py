"""Projection and recomputation — the two tree operations.

A commit lands on the write face; it is then projected down to
``layer/<declared>`` (a subset of the same tree) and every shorter stack that
contains that layer is recomputed (a disjoint union). Both are single-parent
commits, which is why the whole repository has zero merge nodes.

Paths are disjoint by construction, so the union is a plain index concatenation
— no three-way merge, no conflict resolution. See docs/v2/DESIGN.md §6.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import layers as L
from .gitrepo import Entry, Git

TRAILER = "Cb-Stack"


class ProjectionError(RuntimeError):
    pass


@dataclass
class Result:
    projected: list[str]
    stopped: str | None = None
    reason: str | None = None


# ------------------------------------------------------------------ 树运算

def union_tree(git: Git, layers: L.Layers, upto: str) -> str:
    """Disjoint union of ``layer/L1 … layer/upto``.

    ``update-index`` silently lets a later entry overwrite an earlier one, so
    duplicates have to be caught here — and the error message is ours, which is
    the whole reason not to let a merge conflict speak for us.
    """
    entries: list[Entry] = []
    seen: dict[str, str] = {}
    for name in layers.prefix(upto):
        ref = layers.layer_ref(name)
        if git.resolve(ref) is None:
            continue
        for e in git.ls_tree(ref):
            prev = seen.get(e.path)
            if prev is not None:
                raise ProjectionError(f"路径 {e.path} 同时属于层 [{prev}] 和 [{name}]")
            seen[e.path] = name
            entries.append(e)
    return git.write_tree(entries)


def filter_tree(git: Git, commit: str, keep: set[str]) -> str:
    return git.write_tree([e for e in git.ls_tree(commit) if e.path in keep])


# -------------------------------------------------------------------- 游标

def cursor(git: Git) -> str | None:
    return git.resolve(L.PROJECTED_REF)


def set_cursor(git: Git, commit: str) -> None:
    git.update_ref(L.PROJECTED_REF, commit, reason="collectbase: projected")


def pending(git: Git, layers: L.Layers) -> list[str]:
    face = git.resolve(layers.write_face)
    if face is None:
        return []
    at = cursor(git)
    if at is None:
        return [face]
    if at == face:
        return []
    return git.commits_between(at, face)


# ------------------------------------------------------------------ 拓扑

def reconcile(git: Git, layers: L.Layers) -> list[str]:
    """Bring the branch set in line with the anchor.

    The anchor is the only source of truth, so editing it *is* the way to add a
    layer — no special command. Appending at the top moves the write face,
    which the caller has to tell the user about.
    """
    notes: list[str] = []
    for name in layers:
        ref = layers.layer_ref(name)
        if git.resolve(ref) is None:
            oid = git.commit_tree(
                git.empty_tree(), [], f"[{layers.bottom}] collectbase: init layer/{name}"
            )
            git.update_ref(ref, oid, reason="collectbase: new layer")
            notes.append(f"建立 {ref[11:]}")

    for name in layers.stack_names:
        ref = layers.stack_ref(name)
        if git.resolve(ref) is not None:
            continue
        # A brand-new stack just points at the stack below it. The new layer is
        # empty, so the union tree is identical — creating a commit here would
        # mean computing a union from layer/* refs that may not have been
        # projected yet, and that stale tree would then be projected back down.
        tip = _lower_tip(git, layers, name)
        if tip is None:
            continue
        git.update_ref(ref, tip, reason="collectbase: new stack")
        notes.append(f"建立 {ref[11:]}")

    for ref in git.branches("refs/heads/layer/**"):
        name = ref.rsplit("/", 1)[1]
        if name in layers:
            continue
        if git.ls_tree(ref):
            raise ProjectionError(
                f"layers 里去掉了 [{name}],但 {ref[11:]} 还有内容 —— 先把它清空,或把这一层加回去"
            )
        git.delete_ref(ref)
        git.delete_ref(layers.stack_ref(name)) if git.resolve(layers.stack_ref(name)) else None
        notes.append(f"删除空层 {name}")
    return notes


def _lower_tip(git: Git, layers: L.Layers, name: str) -> str | None:
    """The tip a brand-new stack should continue from: the next stack below it,
    or failing that the bottom layer."""
    below = layers.prefix(name)[:-1]
    for lower in reversed(below):
        for ref in (layers.stack_ref(lower), layers.layer_ref(lower)):
            tip = git.resolve(ref)
            if tip is not None:
                return tip
    return None


# -------------------------------------------------------------------- 投影

def run(git: Git, layers: L.Layers | None = None) -> Result:
    """Bring every derived branch up to the write face. Idempotent."""
    layers = layers or L.read(git)
    if layers is None:
        return Result([])
    face = git.resolve(layers.write_face)
    if face is None:
        return Result([])

    done: list[str] = []
    for commit in pending(git, layers):
        tag = L.tag_of(git.subject(commit))
        if tag is None or tag not in layers:
            # Only reachable if something bypassed the guard by writing
            # .git/refs directly. Stop rather than guess: writing this into
            # layer/<facts> would be exactly the corruption we exist to prevent.
            return Result(done, stopped=commit, reason=f"提交 {commit[:7]} 没有合法的 [层名]")
        _project_one(git, layers, commit, tag)
        set_cursor(git, commit)
        done.append(commit)
    return Result(done)


def _project_one(git: Git, layers: L.Layers, commit: str, tag: str) -> None:
    ref = layers.layer_ref(tag)
    tip = git.resolve(ref)

    own = {e.path for e in git.ls_tree(ref)} if tip else set()
    in_commit = {e.path for e in git.ls_tree(commit)}
    for p in git.changed_paths(commit):
        (own.add if p in in_commit else own.discard)(p)

    tree = filter_tree(git, commit, own)
    if tip is None or git.text("rev-parse", f"{tip}^{{tree}}") != tree:
        message = git.message(commit) + f"\n\n{TRAILER}: {commit}\n"
        new = git.commit_tree(tree, [tip] if tip else [], message)
        git.update_ref(ref, new, reason=f"collectbase: project {tag}")

    for name in layers.stacks_containing(tag):
        _recompute(git, layers, name, git.message(commit))


def _recompute(git: Git, layers: L.Layers, name: str, message: str) -> None:
    ref = layers.stack_ref(name)
    tip = git.resolve(ref)
    tree = union_tree(git, layers, name)
    if tip is not None and git.text("rev-parse", f"{tip}^{{tree}}") == tree:
        return
    new = git.commit_tree(tree, [tip] if tip else [], message)
    git.update_ref(ref, new, reason=f"collectbase: recompute {name}")
