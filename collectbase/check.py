"""``cb check`` — the five invariants.

I5 is the reason this command exists at all: the symlink is protected by the
layering rules, the bytes it points at are not. Re-hashing is the only way to
notice a fact's screenshot being swapped. I1–I4 come along for free.

Only the history from the start point onward is checked — collectbase does not
take over your past.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import blob, layers as L, project
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
        _traceable(git, layers),
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
    bad: list[str] = []
    for name in layers.stack_names:
        ref = layers.stack_ref(name)
        tip = git.resolve(ref)
        if tip is None:
            bad.append(f"{ref} 不存在")
            continue
        want = project.union_tree(git, layers, name)
        got = git.text("rev-parse", f"{tip}^{{tree}}")
        if want != got:
            bad.append(f"{ref} 的树与 layer/* 的并集不一致")
    ahead = project.pending(git, layers)
    if ahead:
        bad.append(f"写入面领先投影 {len(ahead)} 个提交")
    return Finding("I2", not bad, "一致:stack/* 的树等于 layer/* 的并集", bad,
                   remedy="cb check 会在下次提交时自动补齐,或直接再提交一次" if ahead else None)


def _linear(git: Git, layers: L.Layers) -> Finding:
    bad = []
    for ref in layers.managed_refs():
        if git.resolve(ref) is None:
            continue
        merges = git.rev_list(ref, "--merges")
        if merges:
            bad.append(f"{ref} 上有 {len(merges)} 个 merge 节点")
    return Finding("I3", not bad, "线性:所有受管分支无 merge 节点", bad)


def _traceable(git: Git, layers: L.Layers) -> Finding:
    start = L.start_point(git)
    missing, total = [], 0
    for name in layers:
        ref = layers.layer_ref(name)
        if git.resolve(ref) is None:
            continue
        spec = f"{start}..{ref}" if start and git.is_ancestor(start, ref) else ref
        for commit in git.rev_list(spec):
            body = git.message(commit)
            if "collectbase: init" in body:
                continue
            total += 1
            if f"{project.TRAILER}:" not in body:
                missing.append(f"{ref[16:]} {commit[:7]} 缺 {project.TRAILER} trailer")
    return Finding("I4", not missing, f"对应:layer/* 的 {total} 个提交都指回写入面", missing[:10])


def _blobs(git: Git) -> Finding:
    report = blob.verify(git)
    details = [f"缺失  {p}  ← {', '.join(sorted(r))}" for p, r in sorted(report.missing.items())]
    details += [f"篡改  {p}  ← {', '.join(sorted(r))}" for p, r in sorted(report.mismatched.items())]
    remedy = None
    if report.missing:
        remedy = "缺失的从别处拉回来:rsync -a <host>:<path>/blob/ blob/"
    if report.mismatched:
        remedy = (remedy + "\n" if remedy else "") + "哈希不匹配意味着有人绕过机制改了字节,不自动处理"
    return Finding("I5", report.ok, f"blob 完整:活集 {report.total} 个", details[:10], remedy)
