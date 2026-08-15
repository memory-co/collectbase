"""守卫不能把 git 自己搞坏。

pack-refs 把 loose ref 迁进 packed-refs 时呈现为一次删除事务;不放行的话
`git gc` 直接 fatal: failed to run pack-refs。
"""

from __future__ import annotations


def test_pack_refs_与_gc_正常(repo):
    repo.write("project/x.md", "x\n")
    repo.commit("[facts] 一个提交")
    before = repo.head()

    assert repo.sh("pack-refs", "--all", check=False).returncode == 0
    out = repo.sh("gc", "-q", check=False)
    assert out.returncode == 0, out.stderr
    assert repo.head() == before
    assert (repo.root / ".git/packed-refs").exists()


def test_gc_之后仓库仍然可用(repo):
    repo.write("project/y.md", "y\n")
    repo.commit("[facts] 提交 1")
    repo.sh("gc", "-q", "--prune=now", check=False)
    repo.write("project/z.md", "z\n")
    assert repo.commit("[facts] 提交 2").returncode == 0


def test_chmod_不产生假_diff(repo):
    """git 只记可执行位,不记写位,所以 444 不会变成一次改动。"""
    from collectbase import blob

    blob.relock(repo.git, ["project/api.md"])
    assert repo.sh("status", "--porcelain").stdout.strip() == ""


def test_删除受管分支不被拦(repo):
    """删除放行是 pack-refs 能工作的前提;内容都还在 layer/* 里。"""
    out = repo.sh("branch", "-D", "stack/notes", check=False)
    assert out.returncode == 0
    assert repo.tree("refs/heads/layer/notes") == []
