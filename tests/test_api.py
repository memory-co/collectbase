"""路径 CRUD:server 用的那一层。它不实现任何规则,规则由守卫说了算。"""

from __future__ import annotations

import os

import pytest

from collectbase import api


def test_读一个软链拿到的是字节_不是软链目标(repo):
    """呈现文件,不呈现 git 对象——调用方不知道 blob 机制存在。"""
    data = b"\x00" + os.urandom(3000)
    repo.on("facts").write("project/shot.png", data)
    repo.commit("[facts] 截图", stay=True)

    assert api.read(repo.git, "project/shot.png") == data
    # git 里存的其实是一条 90 字节的软链
    assert repo.sh("ls-tree", "stack", "project/shot.png").stdout.split()[0] == "120000"


def test_列目录带归属层(repo):
    repo.on("notes").write("project/n.md", "note\n")
    repo.commit("[notes] 笔记", stay=True)

    items = {i.name: i for i in api.listdir(repo.git, "project")}
    assert items["api.md"].layer == "facts"
    assert items["n.md"].layer == "notes"
    assert items["log"].is_dir


def test_视图切换(repo):
    repo.on("notes").write("project/n.md", "note\n")
    repo.commit("[notes] 笔记", stay=True)

    names = {i.name for i in api.listdir(repo.git, "project", view="all")}
    assert {"api.md", "n.md"} <= names
    only_notes = {i.path for i in api.listdir(repo.git, "project", view="notes")}
    assert "project/n.md" in only_notes


def test_blob_条目报的是真实大小(repo):
    data = b"\x00" + os.urandom(5000)
    repo.on("facts").write("project/big.png", data)
    repo.commit("[facts] 图", stay=True)
    item = next(i for i in api.listdir(repo.git, "project") if i.name == "big.png")
    assert item.is_blob and item.size == len(data)


# ------------------------------------------------------------------ 写

def test_一批_ops_一个提交(repo):
    sha = api.commit(repo.git, "notes", "两个文件一起来", [
        api.Put("project/a.md", b"a\n"),
        api.Put("project/b.md", b"b\n"),
    ])
    assert repo.sh("rev-parse", "refs/heads/layer/notes").stdout.strip() == sha
    assert {"project/a.md", "project/b.md"} <= set(repo.own("refs/heads/layer/notes"))
    assert repo.subject("refs/heads/stack") == "[notes] 两个文件一起来"


def test_写入走的是同一条路_落层再_merge(repo):
    sha = api.commit(repo.git, "beliefs", "一条结论", [api.Put("project/why.md", b"x\n")])
    assert repo.sh(
        "merge-base", "--is-ancestor", sha, "refs/heads/stack", check=False
    ).returncode == 0, "权威提交要原样出现在 stack 里"
    tip = repo.sh("rev-parse", "refs/heads/stack").stdout.strip()
    assert len(repo.git.parents(tip)) == 2


def test_写别层占用的路径被守卫拒_信息原样转出(repo):
    with pytest.raises(api.ApiError) as got:
        api.commit(repo.git, "beliefs", "顺手改事实", [api.Put("project/api.md", b"x\n")])
    assert got.value.status == 409
    assert "属于层 [facts]" in str(got.value)


def test_二进制自动转_blob(repo):
    data = b"\x00" + os.urandom(4000)
    api.commit(repo.git, "facts", "一张图", [api.Put("project/pic.png", data)])
    assert repo.sh("ls-tree", "layer/facts", "project/pic.png").stdout.split()[0] == "120000"
    assert api.read(repo.git, "project/pic.png") == data


def test_删除(repo):
    api.commit(repo.git, "notes", "加一个", [api.Put("project/tmp.md", b"x\n")])
    api.commit(repo.git, "notes", "再删掉", [api.Delete("project/tmp.md")])
    assert "project/tmp.md" not in repo.own("refs/heads/layer/notes")


def test_改名是一次提交(repo):
    api.commit(repo.git, "notes", "建立", [api.Put("project/old.md", b"x\n")])
    api.commit(repo.git, "notes", "改名", [
        api.Put("project/new.md", b"x\n"),
        api.Delete("project/old.md"),
    ])
    own = repo.own("refs/heads/layer/notes")
    assert "project/new.md" in own and "project/old.md" not in own


def test_没有改动就报错(repo):
    with pytest.raises(api.ApiError, match="没有任何改动"):
        api.commit(repo.git, "notes", "空的", [])


def test_未知的层(repo):
    with pytest.raises(api.ApiError, match="没有这一层"):
        api.commit(repo.git, "nope", "x", [api.Put("a.md", b"x")])


def test_路径不合法(repo):
    with pytest.raises(api.ApiError, match="路径不合法"):
        api.commit(repo.git, "notes", "x", [api.Put("../escape.md", b"x")])


def test_改_layers_就是加一层_不需要专门的接口(repo):
    api.commit(repo.git, "facts", "加一层 extra",
               [api.Put("layers", b"- facts\n- notes\n- beliefs\n- extra\n")])
    assert repo.sh("rev-parse", "refs/heads/layer/extra", check=False).returncode == 0
    api.commit(repo.git, "extra", "用上新层", [api.Put("project/x.md", b"x\n")])
    assert repo.own("refs/heads/layer/extra") == ["project/x.md"]


def test_写入后工作区仍然是干净的(repo):
    """有人正站在那条分支上时,要把这几个精确路径补过去。"""
    repo.on("notes")
    api.commit(repo.git, "notes", "从 api 写", [api.Put("project/fromapi.md", b"x\n")])
    assert repo.sh("status", "--porcelain").stdout.strip() == ""
    assert (repo.root / "project/fromapi.md").read_text() == "x\n"
