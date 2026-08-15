"""守卫检查的是内容,不是来源。

commit-msg 只覆盖「新写一个提交」这一条路;merge-ff / reset / rebase /
cherry-pick 全都绕得过去(docs/v2/works/exp-refguard.sh 实测)。所以真正的
闸在 reference-transaction 上,规则写在「落进去的每个提交」上,因此对用的是
哪条 git 命令完全无所谓。
"""

from __future__ import annotations

import pytest


def face(repo) -> str:
    return repo.sh("rev-parse", "refs/heads/layer/notes").stdout.strip()


# ----------------------------------------------------------------- 正常路径

def test_合规提交放行(repo):
    repo.write("project/log/2026-08-15.jsonl", '{"event":"ok"}\n')
    assert repo.commit("[facts] 采集 8-15 日志").returncode == 0
    assert repo.subject() == "[facts] 采集 8-15 日志"


def test_一个提交只能属于一层(repo):
    repo.write("project/note.md", "n\n")
    repo.commit("[notes] 一条笔记")
    repo.write("project/note.md", "n2\n")
    repo.write("project/fact.md", "f\n")
    out = repo.commit("[facts] 想顺手把笔记也带上")
    assert out.returncode != 0
    assert "属于层 [notes]" in out.stderr


# ------------------------------------------------- --no-verify 跳不过 ref 守卫

def test_no_verify_没有层标签也被拒(repo):
    repo.write("project/sneak.md", "x\n")
    out = repo.commit("偷偷提交", no_verify=True)
    assert out.returncode != 0
    assert "层名" in out.stderr
    assert repo.subject() != "偷偷提交"


def test_no_verify_改事实层文件也被拒(repo):
    (repo.root / "project/api.md").chmod(0o644)
    repo.write("project/api.md", "tampered\n")
    out = repo.commit("[beliefs] 顺手改事实", no_verify=True)
    assert out.returncode != 0
    assert "属于层 [facts]" in out.stderr


# --------------------------------------------------------- 别的分支进不来

@pytest.fixture
def scratch(repo):
    """一条无标签的草稿分支,外加一个合规的提交。"""
    start = face(repo)
    repo.sh("checkout", "-q", "-b", "scratch", start)
    repo.write("project/draft.md", "draft\n")
    repo.commit("随手写的,没有层标签", no_verify=True)
    untagged = repo.head()
    repo.write("project/good.md", "good\n")
    repo.commit("[notes] 一个合规的提交", no_verify=True)
    tagged = repo.head()
    repo.sh("checkout", "-q", "stack")
    return untagged, tagged


def test_fast_forward_合并被拒(repo, scratch):
    before = face(repo)
    out = repo.sh("merge", "scratch", check=False)
    assert out.returncode != 0
    assert face(repo) == before


def test_no_ff_合并被拒_有_merge_节点(repo, scratch):
    before = face(repo)
    out = repo.sh("merge", "--no-ff", "-m", "merge", "scratch", check=False)
    assert out.returncode != 0
    assert face(repo) == before
    repo.sh("merge", "--abort", check=False)


def test_reset_hard_被拒(repo, scratch):
    before = face(repo)
    out = repo.sh("reset", "--hard", "scratch", check=False)
    assert out.returncode != 0
    assert face(repo) == before


def test_cherry_pick_无标签提交被拒(repo, scratch):
    untagged, _ = scratch
    before = face(repo)
    out = repo.sh("cherry-pick", untagged, check=False)
    assert out.returncode != 0
    assert face(repo) == before
    repo.sh("cherry-pick", "--abort", check=False)


def test_cherry_pick_合规提交放行(repo, scratch):
    """判据是内容,所以合规的 cherry-pick 自然该过,不必为它开特例。"""
    _, tagged = scratch
    out = repo.sh("cherry-pick", tagged, check=False)
    assert out.returncode == 0, out.stderr
    assert repo.subject() == "[notes] 一个合规的提交"
    assert "project/good.md" in repo.tree("refs/heads/layer/notes")


# --------------------------------------------------------------- 自授权

def test_同一提交里加层并使用该层被拒(repo):
    """layers 从 old 读,不是从 new 读——否则提交可以给自己发证。"""
    repo.write("layers", "- facts\n- notes\n- beliefs\n- evil\n")
    repo.write("project/evil.md", "pwned\n")
    out = repo.commit("[evil] 自己给自己发证", no_verify=True)
    assert out.returncode != 0
    assert "未知的层" in out.stderr


def test_加一层就是改锚定(repo):
    """加层不需要专门的命令:锚定是普通路径,改它就是加层。
    stack 的名字是固定的,所以加层不用切分支。"""
    repo.write("layers", "- facts\n- notes\n- beliefs\n- extra\n")
    assert repo.commit("[facts] 增加一层 extra").returncode == 0
    assert repo.sh("rev-parse", "refs/heads/layer/extra", check=False).returncode == 0

    repo.write("project/x.md", "x\n")
    assert repo.commit("[extra] 用上新层").returncode == 0
    assert repo.tree("refs/heads/layer/extra") == ["project/x.md"]


# ------------------------------------------------------- 不受管的分支不挡

def test_草稿分支完全放行(repo):
    repo.sh("checkout", "-q", "-b", "scratch2")
    repo.write("whatever.md", "no tag at all\n")
    assert repo.commit("随便写").returncode == 0

