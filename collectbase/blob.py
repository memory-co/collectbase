"""The blob store: binaries never enter git, only a relative symlink does.

One judgement of "is this binary", used by both the converter (pre-commit)
and the validator (reference-transaction). Two implementations would deadlock
the repo: the converter leaves a file alone, the guard rejects it, and the
commit can never succeed. See docs/v2/works/blob-store.md.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import posixpath
from dataclasses import dataclass
from pathlib import Path

from .gitrepo import Entry, Git

BLOB_DIR = "blob"
SNIFF = 8000
DEFAULT_THRESHOLD = 50 * 1024 * 1024

MODE_FILE = "100644"
MODE_EXEC = "100755"
MODE_LINK = "120000"
MODE_SUBMODULE = "160000"


# --------------------------------------------------------------- 唯一的判据

def is_binary(data: bytes, *, size: int | None = None, threshold: int = DEFAULT_THRESHOLD) -> bool:
    """True when the bytes belong in the blob store rather than in git.

    A NUL byte in the first 8 KB — the same heuristic git itself uses. Verified
    to agree with ``file --mime-encoding`` across the corpus in
    docs/v2/works/blob-store.md §1, including the ``inode/x-empty`` exception
    (an empty file is text here, which is what we want).

    Oversized text is a separate, configurable backstop, not the main rule.
    """
    if size is None:
        size = len(data)
    if size == 0:
        return False
    if b"\0" in data[:SNIFF]:
        return True
    return size > threshold


# ------------------------------------------------------------------- 路径

def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store_path(sha: str, original: str, day: _dt.date | None = None) -> str:
    """``blob/<年>/<月>/<日>/<sha256>.<ext>`` — date shards so the store itself
    is browsable as a media library; sha256 so tampering is detectable."""
    day = day or _dt.date.today()
    ext = posixpath.splitext(original)[1].lstrip(".") or "bin"
    return f"{BLOB_DIR}/{day:%Y/%m/%d}/{sha}.{ext}"


def link_target(from_path: str, blob_path: str) -> str:
    """Relative link from the file's directory to the blob. Relative so the
    repo stays movable; the target carries both the date path and the sha."""
    return posixpath.relpath(blob_path, posixpath.dirname(from_path) or ".")


def resolve_target(from_path: str, target: str) -> str | None:
    """Repo-relative path a symlink points at, or None when it escapes."""
    if posixpath.isabs(target):
        return None
    joined = posixpath.join(posixpath.dirname(from_path), target)
    norm = posixpath.normpath(joined)
    if norm.startswith("../") or norm == "..":
        return None
    return norm


def points_into_store(from_path: str, target: str) -> bool:
    resolved = resolve_target(from_path, target)
    return bool(resolved and (resolved == BLOB_DIR or resolved.startswith(BLOB_DIR + "/")))


def sha_of_store_path(path: str) -> str | None:
    """Pull the sha256 back out of a store path."""
    name = posixpath.basename(path)
    stem = name.split(".", 1)[0]
    return stem if len(stem) == 64 and all(c in "0123456789abcdef" for c in stem) else None


# ---------------------------------------------------------------- 转换器

@dataclass
class Converted:
    path: str
    blob_path: str
    orphan: str | None  # the git object `git add` already wrote, now unreachable


def blobify_worktree(
    git: Git, paths: list[str], *, threshold: int = DEFAULT_THRESHOLD,
    day: _dt.date | None = None,
) -> list[Converted]:
    """Move binary files out of the worktree into the store, leave a symlink.

    Works on the *worktree*, not just the index: ``git commit -a`` stages after
    the hook runs, so index-only edits get overwritten (blob-store.md §2).
    """
    out: list[Converted] = []
    for rel in paths:
        if rel == BLOB_DIR or rel.startswith(BLOB_DIR + "/"):
            continue  # the store itself is never content
        full = git.root / rel
        if full.is_symlink() or not full.is_file():
            continue
        data = full.read_bytes()
        if not is_binary(data, threshold=threshold):
            continue
        orphan = git.text("rev-parse", f":{rel}", check=False) or None
        if orphan and len(orphan) != 40:
            orphan = None

        sha = sha256_of(data)
        dest_rel = store_path(sha, rel, day)
        dest = git.root / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_bytes(data)
            dest.chmod(0o444)
        full.unlink()
        full.symlink_to(link_target(rel, dest_rel))
        # `git add` alone rewrites the index entry 100644 -> 120000;
        # `git rm --cached` first would only error out.
        git.run("add", "--", rel)
        out.append(Converted(rel, dest_rel, orphan))
    return out


def store_symlink(git: Git, path: str, data: bytes, day: _dt.date | None = None) -> Entry:
    """Write bytes into the store and return the symlink entry for ``path``.

    The no-worktree counterpart of :func:`blobify_worktree`, used by the server.
    """
    sha = sha256_of(data)
    dest_rel = store_path(sha, path, day)
    dest = git.root / dest_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_bytes(data)
        dest.chmod(0o444)
    oid = git.hash_object(link_target(path, dest_rel).encode())
    return Entry(MODE_LINK, "blob", oid, path)


def drop_orphans(git: Git, oids: list[str]) -> int:
    """Delete the loose objects `git add` wrote for files we then converted.

    Targeted, because we know the exact oid — no global `git prune`, which
    would also sweep objects another process is mid-way through creating.
    """
    dropped = 0
    for oid in oids:
        if not oid:
            continue
        if git.ok("cat-file", "-e", f"{oid}^{{blob}}") and _unreferenced(git, oid):
            p = git.loose_object_path(oid)
            if p.exists():
                p.unlink()
                dropped += 1
    return dropped


def _unreferenced(git: Git, oid: str) -> bool:
    out = git.text("cat-file", "--batch-check", stdin=b"", check=False)
    del out
    # cheap check: the object must not be reachable from any ref or the index
    if oid in git.text("ls-files", "-s", check=False):
        return False
    found = git.text("rev-list", "--all", "--objects", check=False)
    return oid not in found


# ------------------------------------------------------------------- 活集

def live_set(git: Git) -> dict[str, set[str]]:
    """Every store path referenced by any commit on any ref -> referring paths.

    Liveness comes from history, not the worktree: checking out an old commit
    must still resolve its symlinks.
    """
    live: dict[str, set[str]] = {}
    for commit in git.rev_list("--all"):
        for e in git.ls_tree(commit):
            if e.mode != MODE_LINK:
                continue
            target = git.cat(e.oid).decode(errors="replace")
            resolved = resolve_target(e.path, target)
            if resolved and resolved.startswith(BLOB_DIR + "/"):
                live.setdefault(resolved, set()).add(e.path)
    return live


@dataclass
class BlobReport:
    missing: dict[str, set[str]]
    mismatched: dict[str, set[str]]
    total: int

    @property
    def ok(self) -> bool:
        return not self.missing and not self.mismatched


def verify(git: Git) -> BlobReport:
    """Re-hash every live blob and compare against the sha in its path.

    This is the only way to notice a fact's bytes being swapped: the symlink is
    protected by the layering rules, the bytes it points at are not.
    """
    live = live_set(git)
    missing: dict[str, set[str]] = {}
    mismatched: dict[str, set[str]] = {}
    for path, referrers in live.items():
        full = git.root / path
        if not full.exists():
            missing[path] = referrers
            continue
        want = sha_of_store_path(path)
        if want and sha256_of(full.read_bytes()) != want:
            mismatched[path] = referrers
    return BlobReport(missing, mismatched, len(live))


def gc(git: Git, *, dry_run: bool = False) -> list[str]:
    """Delete store files no commit references any more."""
    live = set(live_set(git))
    root = git.root / BLOB_DIR
    dead: list[str] = []
    if not root.is_dir():
        return dead
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(git.root).as_posix()
        if rel not in live:
            dead.append(rel)
            if not dry_run:
                p.chmod(0o644)
                p.unlink()
    return dead


def relock(git: Git, paths: list[str]) -> None:
    """Re-apply the read-only bit. Git resets permissions whenever it rewrites
    a file, so this has to run after every checkout."""
    for rel in paths:
        full = git.root / rel
        if full.is_file() and not full.is_symlink():
            mode = full.stat().st_mode
            full.chmod(mode & ~0o222)


def unlock(git: Git, paths: list[str]) -> None:
    for rel in paths:
        full = git.root / rel
        if full.is_file() and not full.is_symlink():
            full.chmod(full.stat().st_mode | 0o200)


def worktree_paths_of(git: Git, rev: str) -> list[str]:
    return [e.path for e in git.ls_tree(rev)]


def _unused(*_a, **_k):  # pragma: no cover
    return os.devnull, Path
