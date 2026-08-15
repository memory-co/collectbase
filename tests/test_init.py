"""init 面向的是「已经有东西的仓库」,不是空仓库。"""

from __future__ import annotations

import pytest

from collectbase import init as init_mod, layers as L
from collectbase.gitrepo import Git


def test_既有历史原样成为事实层的历史(repo):
    """不迁移、不重写,一条 ref 就位:layer/facts 直接指向起始点位。"""
    log = repo.sh("log", "--format=%s", "refs/heads/layer/facts").stdout.splitlines()
    assert "pre-existing work" in log
    assert repo.sh(
        "merge-base", "--is-ancestor", "refs/heads/layer/facts", "refs/heads/stack", check=False
    ).returncode == 0


def test_既有内容全部划归最底层(repo):
    """其余层为空,于是 union(layer/*) == layer/facts,不变量当场成立。"""
    assert "project/api.md" in repo.tree("refs/heads/layer/facts")
    assert repo.tree("refs/heads/layer/notes") == []
    assert repo.tree("refs/heads/layer/beliefs") == []
    assert repo.tree("refs/heads/stack") == repo.tree("refs/heads/layer/facts")


def test_起始点位被复用_未新建提交(repo):
    """layer/<底层> 和 main 共享起始点位那一个提交;stack 是各层 tip 的 merge。"""
    start = repo.sh("rev-parse", "refs/heads/main").stdout.strip()
    assert repo.sh("rev-parse", "refs/heads/layer/facts").stdout.strip() == start
    parents = repo.git.parents(repo.sh("rev-parse", "refs/heads/stack").stdout.strip())
    assert start in parents and len(parents) == 3


def test_起始点位是首次引入锚定的提交(repo):
    git = Git(repo.root)
    start = L.start_point(git)
    assert start == repo.sh("rev-parse", "refs/heads/layer/facts").stdout.strip()
    assert "collectbase: init" in repo.subject(start)


def test_起始点位之前的历史不受约束(repo):
    """那条 'pre-existing work' 没有层标签,照样留在历史里。"""
    subjects = repo.sh("log", "--format=%s", "refs/heads/stack").stdout.splitlines()
    assert "pre-existing work" in subjects


def test_切到了写入面并装好了钩子(repo):
    assert repo.sh("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/stack"
    hooks_dir = repo.root / ".git" / "cb-hooks"
    assert repo.sh("config", "core.hooksPath").stdout.strip() == str(hooks_dir)
    for name in init_mod.HOOK_NAMES:
        assert (hooks_dir / name).exists()


def test_钩子不在工作区里(repo):
    """钩子若被跟踪,checkout 到某条权威分支(树里没有它们)就会把它们从工作区
    删掉,守卫随之全部失效——实测过,那时什么都拦不住。"""
    assert not (repo.root / ".collectbase" / "hooks").exists()
    repo.sh("checkout", "-q", "layer/notes")
    assert (repo.root / ".git" / "cb-hooks" / "commit-msg").exists()
    repo.write("hand.md", "x\n")
    out = repo.commit("[beliefs] 层名对不上")
    assert out.returncode != 0, "钩子还在,层名不符要被拒"


def test_下层文件被设成只读(repo):
    repo.sh("checkout", "-q", "stack")
    from collectbase import hooks
    import os

    cwd = os.getcwd()
    os.chdir(repo.root)
    try:
        hooks.post_checkout([])
    finally:
        os.chdir(cwd)
    assert not (repo.root / "project/api.md").stat().st_mode & 0o222


def test_工作区脏时拒绝初始化(bare_repo):
    bare_repo.write("dirty.txt", "x")
    with pytest.raises(init_mod.InitError, match="未提交"):
        init_mod.run(bare_repo.git, ["facts", "notes"])


def test_层名不合法时拒绝(bare_repo):
    with pytest.raises(L.LayersError):
        init_mod.run(bare_repo.git, ["Facts", "notes"])
    with pytest.raises(L.LayersError, match="至少要两层"):
        init_mod.run(bare_repo.git, ["facts"])


def test_clone_之后只补本地配置(repo, tmp_path):
    """core.hooksPath 不随 clone 传播,这是 git 的安全设计。"""
    clone = tmp_path / "clone"
    repo.sh("clone", "-q", str(repo.root), str(clone))
    git = Git(clone)
    assert git.text("config", "--get", "core.hooksPath", check=False) == ""
    plan = init_mod.run(git, ["facts", "notes", "beliefs"])
    assert plan.created == []
    assert git.text("config", "--get", "core.hooksPath").endswith("cb-hooks")
