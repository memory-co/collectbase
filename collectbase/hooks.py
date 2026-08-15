"""The five git hooks.

They are thin: every rule lives in the library, because the server calls the
same functions without any hook firing (docs/v2/works/server.md §2). Two
implementations of the same rule would drift, and the failure mode of drift is
a repository that refuses every commit.

A hook that cannot make sense of the repository stays out of the way — an
unmanaged repo, a detached HEAD, or a branch collectbase does not own all mean
"not our business", never "refuse".
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import blob, layers as L, project, validate
from .gitrepo import Git

HINT_TAG = """
  提交信息要以 [层名] 开头,声明这次改动属于哪一层:
      git commit -m "[{top}] …"
  当前可用的层:{names}
"""

HINT_CROSS = """
  下层是只读的——这是设计,不是权限配置错误。
  想表达"这条记录有问题",在自己层里另写一份并引用它:
      git commit -m "[{top}] 对 {path} 的存疑"

  本次提交未产生任何改动,工作区保持原样。
"""


def _repo() -> Git:
    return Git(Path.cwd())


def _managed(git: Git) -> L.Layers | None:
    """None means: leave this repository alone."""
    layers = L.read(git)
    if layers is None:
        return None
    return layers


# ------------------------------------------------------------------ 入口

def pre_commit(argv: list[str] | None = None) -> int:
    """Turn binaries into blob symlinks before the commit is built."""
    git = _repo()
    layers = _managed(git)
    if layers is None:
        return 0

    staged = _lines(git.text("diff", "--cached", "--name-only", "--diff-filter=ACMRT", check=False))
    # `--diff-filter` must include T: replacing a media file swaps a symlink
    # back to a regular file, which git records as a type change, not M.
    unstaged = _lines(git.text("diff", "--name-only", "--diff-filter=ACMRT", check=False))
    # `git commit -a` stages *after* this hook, so index-only edits would be
    # overwritten; the worktree is the thing that must change.
    converted = blob.blobify_worktree(git, sorted(set(staged) | set(unstaged)))
    if converted:
        state = git.git_dir / "cb-converted"
        with state.open("a") as fh:
            for c in converted:
                fh.write(f"{c.path}\t{c.orphan or ''}\n")
        for c in converted:
            print(f"collectbase: {c.path} → {c.blob_path}", file=sys.stderr)
    return 0


def commit_msg(argv: list[str]) -> int:
    """Fast, friendly rejection. The guarantee is the ref guard; this exists so
    the failure arrives before the commit object does, with a readable message."""
    git = _repo()
    layers = _managed(git)
    if layers is None:
        return 0

    head = git.head_ref()
    if head is None:
        return 0
    if head != layers.write_face:
        if head in layers.managed_refs():
            _say(
                f"✗ 拒绝提交:{head} 是派生分支,由投影生成,不能直接提交。",
                f"  写入面是 {layers.write_face}:",
                f"      git checkout stack/{layers.top}",
            )
            return 1
        return 0  # somebody else's branch — not our business

    path = Path(argv[0]) if argv else None
    subject = (path.read_text().splitlines() or [""])[0] if path else ""
    tag = L.tag_of(subject)

    entries = _staged_entries(git)
    paths = _lines(git.text("diff", "--cached", "--name-only", check=False))
    table = validate.owners(git, layers)
    verdict = validate.check_payload(git, layers, tag, entries, paths, table)
    if verdict.ok:
        return 0

    _say("✗ 拒绝提交。", "")
    for e in verdict.errors:
        _say(f"  {e}")
    if tag is None:
        _say(HINT_TAG.format(top=layers.top, names="、".join(layers.names)))
    else:
        bad = next((p for p in paths if table.get(p.casefold())), "<path>")
        _say(HINT_CROSS.format(top=layers.top, path=bad))
    return 1


def post_commit(argv: list[str] | None = None) -> int:
    """Project, fix the main index after a partial commit, drop orphans."""
    git = _repo()
    layers = _managed(git)
    if layers is None:
        return 0

    state = git.git_dir / "cb-converted"
    if state.exists():
        paths, orphans = [], []
        for line in state.read_text().splitlines():
            p, _, o = line.partition("\t")
            paths.append(p)
            orphans.append(o)
        state.unlink()
        # `git commit -- <path>` hands the hook a temporary index; the main one
        # still holds the pre-conversion entries for everything else.
        for p in paths:
            if (git.root / p).exists():
                git.run("add", "--", p, check=False)
        blob.drop_orphans(git, orphans)

    # The anchor may have changed in this very commit; the branch set has to
    # catch up before anything can be projected.
    face_before = layers.write_face
    try:
        notes = project.reconcile(git, layers)
    except project.ProjectionError as exc:
        _say(f"✗ collectbase:{exc}")
        return 1
    for n in notes:
        _say(f"collectbase: {n}")

    result = project.run(git, layers)
    if result.stopped:
        _say(
            "✗ collectbase:停止投影。",
            f"  {result.reason}",
            "  写入面已越过守卫(多半是有人直接写了 .git/refs)。",
            f"      git revert {result.stopped[:7]}",
        )
        return 1
    if layers.write_face != face_before or git.head_ref() != layers.write_face:
        if git.head_ref() != layers.write_face:
            _say(
                f"collectbase: 写入面现在是 {layers.write_face[11:]},切过去继续:",
                f"    git checkout {layers.write_face[11:]}",
            )
    _relock(git, layers)
    return 0


def post_checkout(argv: list[str] | None = None) -> int:
    """Lower layers read-only. Git resets the mode whenever it rewrites a file,
    so this has to run again after every checkout."""
    git = _repo()
    layers = _managed(git)
    if layers is None:
        return 0
    _relock(git, layers)
    return 0


def reference_transaction(argv: list[str]) -> int:
    """The actual gate. Everything else is ergonomics."""
    if not argv or argv[0] != "prepared":
        return 0
    git = _repo()
    failed = False
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        old, new, ref = parts
        verdict = validate.check_update(git, ref, old, new)
        if not verdict.ok:
            failed = True
            _say(f"✗ collectbase 拒绝更新 {ref}:")
            for e in verdict.errors:
                _say(f"    {e}")
    return 1 if failed else 0


# ------------------------------------------------------------------ 辅助

def _relock(git: Git, layers: L.Layers) -> None:
    """Make the fact layer read-only on disk.

    Only the bottom layer. The floor is the point; the middle layers are not a
    trust boundary, and the write face is shared by everyone who writes — a
    human committing ``[notes]`` should not hit EACCES for it. Ownership is
    still enforced at commit time either way; this is the early signal, not the
    guarantee.
    """
    head = git.head_ref()
    if head != layers.write_face:
        return
    bottom_ref = layers.layer_ref(layers.bottom)
    if git.resolve(bottom_ref) is None:
        return
    floor = [e.path for e in git.ls_tree(bottom_ref) if e.path not in L.META_PATHS]
    face = git.resolve(layers.write_face)
    if face is None:
        return
    above = [e.path for e in git.ls_tree(face) if e.path not in set(floor)]
    blob.unlock(git, above)
    blob.relock(git, floor)


def _staged_entries(git: Git):
    from .gitrepo import Entry

    out = git.run("diff", "--cached", "-z", "--raw", "--no-renames", check=False)
    fields = [f for f in out.split(b"\0") if f]
    entries = []
    i = 0
    while i < len(fields):
        meta = fields[i].decode()
        if not meta.startswith(":"):
            i += 1
            continue
        _src_mode, dst_mode, _src, dst, status = meta[1:].split()
        path = fields[i + 1].decode("utf-8", "surrogateescape")
        if status[0] != "D":
            entries.append(Entry(dst_mode, "blob", dst, path))
        i += 2
    return entries


def _lines(text: str) -> list[str]:
    return [x for x in text.splitlines() if x]


def _say(*lines: str) -> None:
    for line in lines:
        print(line, file=sys.stderr)


ENTRIES = {
    "pre-commit": pre_commit,
    "commit-msg": commit_msg,
    "post-commit": post_commit,
    "post-checkout": post_checkout,
    "reference-transaction": reference_transaction,
}
