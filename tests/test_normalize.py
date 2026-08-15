"""归位:权威在 layer/*,stack 是一串 merge。

两个入口都能提交,hook 负责清理干净——在 stack 上提交的会被拆到权威层,
在 layer 上提交的会被 merge 进 stack。
"""

from __future__ import annotations

from collectbase import check as check_mod, layers as L, normalize


def scenario(repo):
    """先切到权威分支,再改文件,再提交 —— 真实用法就是这样。"""
    repo.on("notes").write("project/api-notes.md", "约束笔记\n")
    repo.commit("[notes] api 的调用约束笔记", stay=True)
    repo.on("beliefs").write("project/why-it-broke.md", "根因判断\n")
    repo.commit("[beliefs] 这次故障的根因判断", stay=True)
    repo.on("facts").write("project/log/2026-08-15.jsonl", '{"e":2}\n')
    repo.commit("[facts] 采集 8-15 日志", stay=True)
    repo.on("beliefs").write("project/why-it-broke.md", "推翻上一版\n")
    repo.commit("[beliefs] 推翻上一版根因", stay=True)


def layers(repo) -> L.Layers:
    got = L.read(repo.git)
    assert got is not None
    return got


# ------------------------------------------------------- 提交打在权威分支上

def test_权威层只含自己的东西(repo):
    """各层都从始祖出发,所以"这一层的东西"= 相对始祖的增量。"""
    scenario(repo)
    assert repo.own("refs/heads/layer/notes") == ["project/api-notes.md"]
    assert repo.own("refs/heads/layer/beliefs") == ["project/why-it-broke.md"]
    assert repo.own("refs/heads/layer/facts") == ["project/log/2026-08-15.jsonl"]


def test_布局可以交错(repo):
    """分层的价值就在于上层能把文件放在下层的文件旁边。"""
    scenario(repo)
    assert "project/api.md" in repo.tree("refs/heads/layer/facts")
    assert "project/api-notes.md" in repo.own("refs/heads/layer/notes")


def test_stack_上每次改动只有一个_merge_节点(repo):
    before = len(repo.sh("rev-list", "--first-parent", "refs/heads/stack").stdout.splitlines())
    repo.on("notes").write("project/n.md", "n\n")
    repo.commit("[notes] 一条笔记", stay=True)
    after = repo.sh("rev-list", "--first-parent", "refs/heads/stack").stdout.splitlines()
    assert len(after) == before + 1
    assert len(repo.git.parents(after[0])) == 2, "stack 的 tip 应当是 merge"


def test_权威提交的_SHA_原样进入_stack(repo):
    """不是复制一份,是同一个对象。"""
    repo.on("notes").write("project/n.md", "n\n")
    repo.commit("[notes] 一条笔记", stay=True)
    mine = repo.sh("rev-parse", "refs/heads/layer/notes").stdout.strip()
    assert mine in repo.git.parents(repo.sh("rev-parse", "refs/heads/stack").stdout.strip())
    assert repo.sh(
        "merge-base", "--is-ancestor", mine, "refs/heads/stack", check=False
    ).returncode == 0


def test_在_stack_上提交被拒(repo):
    repo.sh("checkout", "-q", "stack")
    repo.write("x.md", "x\n")
    repo.sh("add", "-A")
    out = repo.sh("commit", "-q", "-m", "[notes] 在 stack 上写", check=False)
    assert out.returncode != 0
    assert "只接收 merge" in out.stderr


def test_merge_节点的信息与权威提交逐字相同(repo):
    repo.on("notes").write("project/n.md", "n\n")
    repo.commit("[notes] api 的调用约束笔记", stay=True)
    assert repo.subject("refs/heads/stack") == "[notes] api 的调用约束笔记"
    assert repo.subject("refs/heads/layer/notes") == "[notes] api 的调用约束笔记"


def test_stack_的树等于各层并集(repo):
    scenario(repo)
    tip = repo.sh("rev-parse", "refs/heads/stack^{tree}").stdout.strip()
    assert tip == normalize.union_tree(repo.git, layers(repo))


def test_权威分支线性无_merge(repo):
    scenario(repo)
    for name in ("facts", "notes", "beliefs"):
        assert repo.sh("rev-list", "--merges", f"refs/heads/layer/{name}").stdout.strip() == ""


def test_stack_是全局时间线且自带层标注(repo):
    scenario(repo)
    subjects = repo.sh("log", "--format=%s", "--first-parent", "refs/heads/stack").stdout.splitlines()
    assert subjects[:4] == [
        "[beliefs] 推翻上一版根因",
        "[facts] 采集 8-15 日志",
        "[beliefs] 这次故障的根因判断",
        "[notes] api 的调用约束笔记",
    ]


def test_只看某一层的历史(repo):
    scenario(repo)
    beliefs = repo.sh("log", "--format=%s", "refs/heads/layer/beliefs").stdout.splitlines()
    assert beliefs[:2] == ["[beliefs] 推翻上一版根因", "[beliefs] 这次故障的根因判断"]


# --------------------------------------------------------------- 层名对不上

def test_在权威分支上声明别的层会被拒(repo):
    repo.sh("checkout", "-q", "layer/notes")
    repo.write("x.md", "x\n")
    out = repo.commit("[beliefs] 层名对不上", stay=True)
    assert out.returncode != 0
    assert "却声明了 [beliefs]" in out.stderr


def test_事实层可写在自己的分支上(repo):
    repo.write("project/api.md", "改事实,在事实层上就是合法的\n")
    assert repo.commit("[facts] 修正记录").returncode == 0


# ----------------------------------------------------------------- 其余

def test_删自己层的文件是合法的(repo):
    scenario(repo)
    repo.on("beliefs")
    (repo.root / "project/why-it-broke.md").unlink()
    assert repo.commit("[beliefs] 收回这条结论", stay=True).returncode == 0
    assert repo.own("refs/heads/layer/beliefs") == []


def test_删下层的文件被拒(repo):
    scenario(repo)
    repo.on("beliefs")
    (repo.root / "project/api.md").chmod(0o644)
    (repo.root / "project/api.md").unlink()
    out = repo.commit("[beliefs] 删掉一个事实", stay=True)
    assert out.returncode != 0
    assert "属于层 [facts]" in out.stderr


def test_check_全绿(repo):
    scenario(repo)
    report = check_mod.run(repo.git)
    failed = [f.id for f in report.findings if not f.ok]
    assert failed == [], [f.details for f in report.findings if not f.ok]


def test_stack_坏了可以重建(repo):
    """stack 是构建产物,不是权威。"""
    scenario(repo)
    want = repo.sh("rev-parse", "refs/heads/stack^{tree}").stdout.strip()
    repo.sh("update-ref", "refs/heads/stack", repo.sh("rev-parse", "refs/heads/layer/facts").stdout.strip())
    assert not check_mod.run(repo.git).ok

    normalize.rebuild(repo.git)
    assert repo.sh("rev-parse", "refs/heads/stack^{tree}").stdout.strip() == want
    assert check_mod.run(repo.git).ok
