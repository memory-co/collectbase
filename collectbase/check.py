"""``cb check`` —— 四条不变量。

I4 是这条命令存在的理由:软链受分层规则保护,它指向的字节不受。重新哈希是
唯一能发现"作为事实的截图被悄悄换掉"的手段。I1–I3 顺手一起验,反正代码都在。

只校验起始点位之后的历史——collectbase 不接管你的过去。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import blob, layers as L, normalize
from .gitrepo import Git


@dataclass
class Finding:
    id: str
    ok: bool
    summary: str
    details: list[str] = field(default_factory=list)
    remedy: str | None = None


@dataclass
class Report:
    start_point: str | None
    findings: list[Finding]

    @property
    def ok(self) -> bool:
        return all(f.ok for f in self.findings)


def run(git: Git) -> Report:
    layers = L.read(git)
    if layers is None:
        return Report(None, [Finding("init", False, "仓库尚未初始化", remedy="cb init --layers …")])

    start = L.start_point(git)
    return Report(start, [
        _disjoint(git, layers),
        _consistent(git, layers),
        _linear(git, layers),
        _blobs(git),
    ])


def _disjoint(git: Git, layers: L.Layers) -> Finding:
    seen: dict[str, str] = {}
    clashes: list[str] = []
    total = 0
    for name in layers:
        ref = layers.layer_ref(name)
        if git.resolve(ref) is None:
            continue
        for e in git.ls_tree(ref):
            total += 1
            prev = seen.get(e.path.casefold())
            if prev:
                clashes.append(f"{e.path} 同时属于 [{prev}] 和 [{name}]")
            seen[e.path.casefold()] = name
    return Finding("I1", not clashes,
                   f"不相交:{len(layers.names)} 层共 {total} 个路径" + ("" if not clashes else ",有重叠"),
                   clashes)


def _consistent(git: Git, layers: L.Layers) -> Finding:
    """stack 是构建产物;它落后或损坏都不致命,重建即可。"""
    bad: list[str] = []
    tip = git.resolve(layers.stack_ref)
    if tip is None:
        bad.append("stack 不存在")
    else:
        if git.text("rev-parse", f"{tip}^{{tree}}") != normalize.union_tree(git, layers):
            bad.append("stack 的树与各层 tip 的并集不一致")
        for name in layers:
            lt = git.resolve(layers.layer_ref(name))
            if lt and not git.is_ancestor(lt, tip):
                bad.append(f"stack 还没 merge 进 layer/{name}")
    return Finding("I2", not bad, "一致:stack 的树等于各层 tip 的并集,且含各层为祖先", bad,
                   remedy="stack 是构建产物,重建即可:再提交一次,或 cb rebuild")


def _linear(git: Git, layers: L.Layers) -> Finding:
    """权威分支必须线性。stack 全是 merge,那是它的本分。"""
    bad = []
    for name in layers:
        ref = layers.layer_ref(name)
        if git.resolve(ref) is None:
            continue
        merges = git.rev_list(ref, "--merges")
        if merges:
            bad.append(f"layer/{name} 上有 {len(merges)} 个 merge 节点")
    return Finding("I3", not bad, "线性:每条权威分支都没有 merge 节点", bad)


def _blobs(git: Git) -> Finding:
    report = blob.verify(git)
    details = [f"缺失  {p}  ← {', '.join(sorted(r))}" for p, r in sorted(report.missing.items())]
    details += [f"篡改  {p}  ← {', '.join(sorted(r))}" for p, r in sorted(report.mismatched.items())]
    remedy = None
    if report.missing:
        remedy = "缺失的从别处拉回来:rsync -a <host>:<path>/blob/ blob/"
    if report.mismatched:
        remedy = (remedy + "\n" if remedy else "") + "哈希不匹配意味着有人绕过机制改了字节,不自动处理"
    return Finding("I4", report.ok, f"blob 完整:活集 {report.total} 个", details[:10], remedy)
