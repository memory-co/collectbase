from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from collectbase import init as init_mod
from collectbase.gitrepo import Git

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    for k, v in {
        "GIT_AUTHOR_NAME": "cb",
        "GIT_AUTHOR_EMAIL": "cb@example.com",
        "GIT_COMMITTER_NAME": "cb",
        "GIT_COMMITTER_EMAIL": "cb@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }.items():
        monkeypatch.setenv(k, v)
    # the tracked hook uses `#!/usr/bin/env python3`, so that interpreter has
    # to be able to import collectbase — exactly the constraint the design
    # calls out in works/cli.md §3.
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", f"{PROJECT_ROOT}:{existing}" if existing else str(PROJECT_ROOT))
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}:{os.environ['PATH']}")


class Repo:
    """A managed repository, driven the way a user would: plain git."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.git = Git(root)

    def sh(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=check
        )

    def write(self, rel: str, data: str | bytes) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            p.write_text(data)
        else:
            p.write_bytes(data)
        return p

    def commit(self, message: str, *paths: str, no_verify: bool = False):
        self.sh("add", "-A", "--", *(paths or ["."]), check=False)
        args = ["commit", "-q", "-m", message]
        if no_verify:
            args.insert(1, "--no-verify")
        return self.sh(*args, check=False)

    def subject(self, rev: str = "HEAD") -> str:
        return self.sh("log", "-1", "--format=%s", rev).stdout.strip()

    def tree(self, rev: str) -> list[str]:
        out = self.sh("ls-tree", "-r", "--name-only", rev).stdout
        return sorted(x for x in out.splitlines() if x)

    def head(self) -> str:
        return self.sh("rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def bare_repo(tmp_path) -> Repo:
    """An ordinary repository with prior history — the default init case."""
    root = tmp_path / "repo"
    root.mkdir()
    repo = Repo(root)
    repo.sh("init", "-q", "-b", "main")
    repo.write("project/api.md", "the api\n")
    repo.write("project/log/2026-08-14.jsonl", '{"event":"start"}\n')
    repo.write("README.md", "# existing project\n")
    repo.sh("add", "-A")
    repo.sh("commit", "-q", "-m", "pre-existing work")
    return repo


@pytest.fixture
def repo(bare_repo) -> Repo:
    init_mod.run(bare_repo.git, ["facts", "notes", "beliefs"])
    return bare_repo
