"""投影 + 重算:单亲提交,全仓库零 merge 节点。"""

from __future__ import annotations

from collectbase import check as check_mod, project


def scenario(repo):
    """事实层 → notes → beliefs → 事实层继续前进 → beliefs 修正自己。"""
    repo.write("project/api-notes.md", "约束笔记\n")
    repo.commit("[notes] api 的调用约束笔记")
    repo.write("project/why-it-broke.md", "根因判断\n")
    repo.commit("[beliefs] 这次故障的根因判断")
    repo.write("project/log/2026-08-15.jsonl", '{"e":2}\n')
    repo.commit("[facts] 采集 8-15 日志")
    repo.write("project/why-it-broke.md", "推翻上一版\n")
    repo.commit("[beliefs] 推翻上一版根因")


def test_各层只含自己的文件(repo):
    scenario(repo)
    assert repo.tree("refs/heads/layer/notes") == ["project/api-notes.md"]
    assert repo.tree("refs/heads/layer/beliefs") == ["project/why-it-broke.md"]
    facts = repo.tree("refs/heads/layer/facts")
    assert "project/api.md" in facts and "project/api-notes.md" not in facts


def test_布局可以交错(repo):
    """分层的价值就在于 layer2 能把文件放在 layer1 的文件旁边。"""
    scenario(repo)
    assert "project/api.md" in repo.tree("refs/heads/layer/facts")
    assert "project/api-notes.md" in repo.tree("refs/heads/layer/notes")
    assert "project/log/2026-08-15.jsonl" in repo.tree("refs/heads/layer/facts")


def test_全仓库没有_merge_节点(repo):
    scenario(repo)
    out = repo.sh("rev-list", "--merges", "--all").stdout.strip()
    assert out == ""


def test_每次提交后所有分支都是_fast_forward(repo):
    refs = ["refs/heads/stack/beliefs", "refs/heads/stack/notes",
            "refs/heads/layer/facts", "refs/heads/layer/notes", "refs/heads/layer/beliefs"]
    before = {r: repo.sh("rev-parse", r).stdout.strip() for r in refs}
    scenario(repo)
    for r in refs:
        after = repo.sh("rev-parse", r).stdout.strip()
        assert repo.sh("merge-base", "--is-ancestor", before[r], after, check=False).returncode == 0


def test_不变量_I2_成立(repo):
    scenario(repo)
    for name in ("notes", "beliefs"):
        tip = repo.sh("rev-parse", f"refs/heads/stack/{name}^{{tree}}").stdout.strip()
        want = project.union_tree(repo.git, repo.git and _layers(repo), name)
        assert tip == want


def _layers(repo):
    from collectbase import layers as L

    got = L.read(repo.git)
    assert got is not None
    return got


def test_写入面是全局时间线且自带层标注(repo):
    scenario(repo)
    subjects = repo.sh("log", "--format=%s", "refs/heads/stack/beliefs").stdout.splitlines()
    tagged = [s for s in subjects if s.startswith("[")]
    assert tagged[:4] == [
        "[beliefs] 推翻上一版根因",
        "[facts] 采集 8-15 日志",
        "[beliefs] 这次故障的根因判断",
        "[notes] api 的调用约束笔记",
    ]


def test_layer_历史是写入面历史的子序列(repo):
    scenario(repo)
    beliefs = repo.sh("log", "--format=%s", "refs/heads/layer/beliefs").stdout.splitlines()
    assert beliefs[:2] == ["[beliefs] 推翻上一版根因", "[beliefs] 这次故障的根因判断"]


def test_投影提交带_Cb_Stack_trailer(repo):
    scenario(repo)
    body = repo.sh("log", "-1", "--format=%B", "refs/heads/layer/beliefs").stdout
    assert "Cb-Stack:" in body


def test_只有该层的提交才推进对应分支(repo):
    scenario(repo)
    notes_log = repo.sh("log", "--format=%s", "refs/heads/layer/notes").stdout.splitlines()
    assert not any("[facts]" in s or "[beliefs]" in s for s in notes_log[:1])


def test_check_全绿(repo):
    scenario(repo)
    report = check_mod.run(repo.git)
    failed = [f.id for f in report.findings if not f.ok]
    assert failed == [], [f.details for f in report.findings if not f.ok]


def test_删除也走同一条规则(repo):
    scenario(repo)
    (repo.root / "project/api.md").chmod(0o644)
    (repo.root / "project/api.md").unlink()
    out = repo.commit("[beliefs] 删掉一个事实")
    assert out.returncode != 0
    assert "属于层 [facts]" in out.stderr


def test_删自己层的文件是合法的(repo):
    scenario(repo)
    (repo.root / "project/why-it-broke.md").unlink()
    assert repo.commit("[beliefs] 收回这条结论").returncode == 0
    assert repo.tree("refs/heads/layer/beliefs") == []
