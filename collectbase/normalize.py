"""归位:把每一次改动放到它的权威层分支上,再 merge 进 ``stack``。

``layer/<name>`` 是**唯一的权威**——每条只放自己这一层的文件,线性、必须 FF。
``stack`` 是**构建产物**:一串 merge,树是各层 tip 的并集,merge 节点的信息与
权威提交逐字相同。它同样设防(每个提交都要带合法 ``[层名]``、内容过白名单),
只是不要求 FF——归位时会改写它;真坏了重建即可,原料是权威分支。

两个入口都行,hook 负责把它清理干净:

* 在 ``layer/<L>`` 上提交 —— 本来就是权威的,merge 进 stack 就完了
* 在 ``stack`` 上提交 —— 那个提交不是权威的,把属于声明层的子集拆出来提交到
  ``layer/<L>``,再把 stack 改写成本该在那儿的那个 merge 节点

见 docs/v2/DESIGN.md §6。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import layers as L
from .gitrepo import Entry, Git


class NormalizeError(RuntimeError):
    pass


@dataclass
class Result:
    merged: list[str]
    extracted: str | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.reason is None


# ------------------------------------------------------------------ 树运算

def merge_tree(git: Git, a: str, b: str) -> str:
    """两条分支的合并树,由 **git 自己算**。

    各层都从始祖出发,此后各加各的文件,所以三方合并的两侧改动不相交,永不
    冲突。冲突意味着 I1 被破坏了——那不是待处理的日常,是损坏信号。
    """
    out = git.run("merge-tree", "--write-tree", a, b, check=False)
    tree = out.decode(errors="replace").strip().splitlines()
    if not tree or len(tree[0]) != 40:
        raise NormalizeError(f"合并 {a[:7]} 与 {b[:7]} 冲突了 —— 说明有路径同时属于两层")
    return tree[0]


def union_tree(git: Git, layers: L.Layers) -> str:
    """各层的合并树。只有 ``cb rebuild`` 用得上——日常路径是 git 的 merge。"""
    tips = [t for t in (git.resolve(layers.layer_ref(n)) for n in layers) if t]
    if not tips:
        raise NormalizeError("一条层分支都没有")
    tree = git.text("rev-parse", f"{tips[0]}^{{tree}}")
    acc = tips[0]
    for tip in tips[1:]:
        tree = merge_tree(git, acc, tip)
        acc = git.commit_tree(tree, [acc, tip], "[cb] tmp")
    return tree


# -------------------------------------------------------------------- 归位

def run(git: Git, layers: L.Layers | None = None) -> Result:
    """把一切放回它该在的位置。幂等,想跑几次跑几次。"""
    layers = layers or L.read(git)
    if layers is None:
        return Result([])

    extracted = None
    if git.head_ref() == layers.stack_ref:
        try:
            extracted = _extract(git, layers)
        except NormalizeError as exc:
            return Result([], reason=str(exc))
    try:
        return _merge_pending(git, layers, extracted)
    except NormalizeError as exc:
        return Result([], extracted=extracted, reason=str(exc))


def _extract(git: Git, layers: L.Layers) -> str | None:
    """把落在 stack 上的那次改动搬到它的权威层分支上。

    人会在 stack 上提交,因为**只有那个工作区同时看得见所有层**。但那个提交
    不是权威的,所以要把**这一次改动**应用到 ``layer/<L>`` 上——一个提交只能
    属于一层(守卫保证),所以它的 diff 本来就只碰那一层的路径,搬过去就是
    一次 cherry-pick,由 git 算。然后 stack 退回提交前,让 merge 节点顶上去。
    """
    tip = git.resolve("HEAD")
    if tip is None or len(git.parents(tip)) != 1:
        return None  # 根提交或已经是 merge —— 没什么可拆的

    tag = L.tag_of(git.subject(tip))
    if tag is None or tag not in layers:
        raise NormalizeError(f"提交 {tip[:7]} 没有合法的 [层名],无法归位")

    layer_ref = layers.layer_ref(tag)
    layer_tip = git.resolve(layer_ref)

    tree = git.apply_onto(tip, layer_ref) if layer_tip else git.text("rev-parse", f"{tip}^{{tree}}")

    if layer_tip is not None and git.text("rev-parse", f"{layer_tip}^{{tree}}") == tree:
        new_layer = layer_tip  # 内容没变(比如只改了别层的东西)
    else:
        new_layer = git.commit_tree(tree, [layer_tip] if layer_tip else [], git.message(tip))
        git.update_ref(layer_ref, new_layer, reason=f"collectbase: extract → {tag}")

    git.update_ref(layers.stack_ref, git.parents(tip)[0], reason="collectbase: normalise")
    return new_layer


def _merge_pending(git: Git, layers: L.Layers, prefer: str | None) -> Result:
    """把 stack 还没追上的层 tip 逐个 merge 进来。

    merge 节点的信息与权威提交**逐字相同**,所以 ``git log stack`` 读起来就是
    那条时间线本身,不是一串 "Merge branch …"。
    """
    merged: list[str] = []
    names = list(layers)
    if prefer is not None:
        # 刚归位的那一层**最后**merge —— stack 的 tip 消息才是这次真正的改动,
        # 而不是某条顺带追上的空层分支。
        names.sort(key=lambda n: git.resolve(layers.layer_ref(n)) == prefer)

    for name in names:
        tip = git.resolve(layers.layer_ref(name))
        if tip is None:
            continue
        stack = git.resolve(layers.stack_ref)
        if stack is not None and git.is_ancestor(tip, stack):
            continue
        tree = merge_tree(git, stack, tip) if stack else git.text("rev-parse", f"{tip}^{{tree}}")
        parents = [p for p in (stack, tip) if p]
        new = git.commit_tree(tree, parents, git.message(tip))
        git.update_ref(layers.stack_ref, new, reason=f"collectbase: merge {name}")
        merged.append(name)
    return Result(merged, extracted=prefer)


def reconcile(git: Git, layers: L.Layers) -> list[str]:
    """按锚定调整分支集合。加一层就是往 ``layers`` 里加一行,没有专门的命令。

    只给 ref 改指向、建空分支,**绝不推导树**——reconcile 跑在归位之前,那时
    ``layer/*`` 可能还没追平,拿它算出来的树是陈旧的。
    """
    notes: list[str] = []
    base = L.start_point(git)
    for name in layers:
        ref = layers.layer_ref(name)
        if git.resolve(ref) is None:
            if base is None:
                continue
            # 新层也从始祖出发 —— 和 init 时一样,一条 ref 就位。
            git.update_ref(ref, base, reason="collectbase: new layer")
            notes.append(f"建立 {ref[11:]}")

    for ref in git.branches("refs/heads/layer/**"):
        name = ref.rsplit("/", 1)[1]
        if name in layers:
            continue
        base_paths = {e.path for e in git.ls_tree(base)} if base else set()
        if {e.path for e in git.ls_tree(ref)} - base_paths:
            raise NormalizeError(
                f"layers 里去掉了 [{name}],但 {ref[11:]} 还有内容 —— 先清空它,或把这一层加回去"
            )
        git.delete_ref(ref)
        notes.append(f"删除空层 {name}")
    return notes


def rebuild(git: Git, layers: L.Layers | None = None) -> str:
    """从各层 tip 重建 stack。它是构建产物,这么做永远是安全的。"""
    layers = layers or L.read(git)
    if layers is None:
        raise NormalizeError("仓库尚未初始化")
    tree = union_tree(git, layers)
    parents = [t for t in (git.resolve(layers.layer_ref(n)) for n in layers) if t]
    new = git.commit_tree(tree, parents, f"[{layers.bottom}] collectbase: rebuild stack")
    git.update_ref(layers.stack_ref, new, reason="collectbase: rebuild")
    return new
