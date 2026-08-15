"""Thin wrapper over git plumbing.

Everything collectbase does to a repository goes through here: read trees,
synthesize trees, write commits, move refs. No porcelain, no working-tree
assumptions — the server has no worktree at all (docs/v2/works/server.md §2).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

ZERO = "0" * 40


class GitError(RuntimeError):
    def __init__(self, args: list[str], code: int, stderr: str) -> None:
        super().__init__(f"git {' '.join(args)} -> {code}: {stderr.strip()}")
        self.code = code
        self.stderr = stderr


@dataclass(frozen=True)
class Entry:
    """One tree entry. ``mode`` is the string git uses: 100644 / 120000 / …"""

    mode: str
    kind: str  # blob | tree | commit
    oid: str
    path: str

    def line(self) -> str:
        return f"{self.mode} {self.kind} {self.oid}\t{self.path}"


class Git:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    # ------------------------------------------------------------ invocation

    def run(self, *args: str, stdin: bytes | None = None, check: bool = True) -> bytes:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.root,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if check and proc.returncode != 0:
            raise GitError(list(args), proc.returncode, proc.stderr.decode(errors="replace"))
        return proc.stdout

    def text(self, *args: str, stdin: bytes | None = None, check: bool = True) -> str:
        return self.run(*args, stdin=stdin, check=check).decode(errors="replace").strip()

    def ok(self, *args: str) -> bool:
        return subprocess.run(
            ["git", *args], cwd=self.root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode == 0

    # ------------------------------------------------------------------ refs

    @property
    def git_dir(self) -> Path:
        return Path(self.text("rev-parse", "--absolute-git-dir"))

    def resolve(self, rev: str) -> str | None:
        try:
            return self.text("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}") or None
        except GitError:
            return None

    def update_ref(self, ref: str, new: str, old: str | None = None, reason: str = "collectbase") -> None:
        args = ["update-ref", "-m", reason, ref, new]
        if old is not None:
            args.append(old)
        self.run(*args)

    def delete_ref(self, ref: str) -> None:
        self.run("update-ref", "-d", ref)

    def branches(self, pattern: str = "refs/heads/**") -> list[str]:
        out = self.text("for-each-ref", "--format=%(refname)", pattern)
        return [x for x in out.splitlines() if x]

    def head_ref(self) -> str | None:
        """Symbolic ref of HEAD, or None when detached."""
        try:
            return self.text("symbolic-ref", "--quiet", "HEAD") or None
        except GitError:
            return None

    def is_ancestor(self, old: str, new: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", old, new)

    # ----------------------------------------------------------------- trees

    def ls_tree(self, rev: str, *paths: str) -> list[Entry]:
        out = self.run("ls-tree", "-r", "-z", rev, "--", *paths)
        entries = []
        for chunk in out.split(b"\0"):
            if not chunk:
                continue
            meta, path = chunk.split(b"\t", 1)
            mode, kind, oid = meta.decode().split()
            entries.append(Entry(mode, kind, oid, path.decode("utf-8", "surrogateescape")))
        return entries

    def tree_map(self, rev: str) -> dict[str, Entry]:
        return {e.path: e for e in self.ls_tree(rev)}

    def write_tree(self, entries: list[Entry]) -> str:
        """Build a tree from an explicit entry list, via a scratch index."""
        idx = self.git_dir / f"cb-index-{id(entries):x}"
        payload = b"".join(e.line().encode("utf-8", "surrogateescape") + b"\0" for e in entries)
        try:
            proc = subprocess.run(
                ["git", "update-index", "-z", "--index-info"],
                cwd=self.root,
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**_env(), "GIT_INDEX_FILE": str(idx)},
            )
            if proc.returncode != 0:
                raise GitError(["update-index"], proc.returncode, proc.stderr.decode())
            proc = subprocess.run(
                ["git", "write-tree"],
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**_env(), "GIT_INDEX_FILE": str(idx)},
            )
            if proc.returncode != 0:
                raise GitError(["write-tree"], proc.returncode, proc.stderr.decode())
            return proc.stdout.decode().strip()
        finally:
            idx.unlink(missing_ok=True)

    def empty_tree(self) -> str:
        return self.text("hash-object", "-t", "tree", "/dev/null")

    # --------------------------------------------------------------- objects

    def cat(self, oid: str) -> bytes:
        return self.run("cat-file", "blob", oid)

    def cat_head(self, oid: str, n: int) -> bytes:
        """First ``n`` bytes of a blob — enough for the binary sniff, without
        materialising a 500 MB object just to look for a NUL."""
        proc = subprocess.Popen(
            ["git", "cat-file", "blob", oid],
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        try:
            return proc.stdout.read(n)
        finally:
            proc.stdout.close()
            proc.wait()

    def size(self, oid: str) -> int:
        return int(self.text("cat-file", "-s", oid))

    def hash_object(self, data: bytes) -> str:
        return self.text("hash-object", "-w", "--stdin", stdin=data)

    def loose_object_path(self, oid: str) -> Path:
        return self.git_dir / "objects" / oid[:2] / oid[2:]

    # --------------------------------------------------------------- commits

    def commit_tree(self, tree: str, parents: list[str], message: str) -> str:
        args = ["commit-tree", tree]
        for p in parents:
            args += ["-p", p]
        return self.text(*args, stdin=message.encode())

    def message(self, commit: str) -> str:
        return self.run("log", "-1", "--format=%B", commit).decode(errors="replace").rstrip("\n")

    def subject(self, commit: str) -> str:
        return self.text("log", "-1", "--format=%s", commit)

    def parents(self, commit: str) -> list[str]:
        return self.text("rev-list", "-1", "--parents", commit).split()[1:]

    def rev_list(self, spec: str, *extra: str) -> list[str]:
        out = self.text("rev-list", spec, *extra)
        return [x for x in out.splitlines() if x]

    def commits_between(self, old: str, new: str) -> list[str]:
        """Commits reachable from ``new`` but not ``old``, oldest first."""
        return self.rev_list(f"{old}..{new}", "--reverse")

    def changed_entries(self, commit: str) -> list[Entry]:
        """Entries added or modified by ``commit`` (deletions excluded)."""
        parents = self.parents(commit)
        base = parents[0] if parents else self.empty_tree()
        out = self.run("diff-tree", "-r", "-z", "--no-commit-id", base, commit)
        fields = [f for f in out.split(b"\0") if f]
        entries: list[Entry] = []
        i = 0
        while i < len(fields):
            meta = fields[i].decode()
            if not meta.startswith(":"):
                i += 1
                continue
            _src_mode, dst_mode, _src, dst, status = meta[1:].split()
            # rename/copy carry two paths
            take = 2 if status[0] in "RC" else 1
            path = fields[i + take].decode("utf-8", "surrogateescape")
            if status[0] != "D":
                entries.append(Entry(dst_mode, "blob", dst, path))
            i += 1 + take
        return entries

    def changed_paths(self, commit: str) -> list[str]:
        """All paths touched by ``commit``, deletions included."""
        parents = self.parents(commit)
        base = parents[0] if parents else self.empty_tree()
        out = self.run("diff-tree", "-r", "-z", "--no-commit-id", "--name-only", base, commit)
        return [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]


def _env() -> dict[str, str]:
    import os

    return dict(os.environ)
