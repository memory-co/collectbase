"""守卫:只保护权威分支,检查落进去的每个提交。

`commit-msg` 只覆盖「新写一个提交」这一条路;merge-ff / reset / rebase /
cherry-pick 全都绕得过去(docs/v2/works/exp-refguard.sh 实测)。所以真正的闸
在 `reference-transaction` 上,规则写在**内容**上,因此对用的是哪条 git 命令
完全无所谓。

守的是 `layer/*`。`stack` 是构建产物,不守——它坏了 `cb check` 会报,重建
即可,而重建的原料是权威分支,不是它自己。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import blob, layers as L
from .gitrepo import ZERO, Entry, Git


@dataclass
class Verdict:
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def fail(self, msg: str) -> None:
        self.errors.append(msg)


def owners(git: Git, layers: L.Layers) -> dict[str, tuple[str, str]]:
    """casefold(path) -> (层, 真实路径)。

    折叠大小写:在大小写不敏感的文件系统上 ``Facts/a.md`` 和 ``facts/a.md``
    是两个 git 路径、同一个磁盘文件。
    """
    base = L.start_point(git)
    base_paths = {e.path for e in git.ls_tree(base)} if base else set()
    table: dict[str, tuple[str, str]] = {}
    for p in base_paths:
        table[p.casefold()] = (layers.bottom, p)  # 始祖里的东西是共同的基,归最底层
    for name in layers:
        ref = layers.layer_ref(name)
        if git.resolve(ref) is None:
            continue
        for e in git.ls_tree(ref):
            if e.path in base_paths:
                continue
            table[e.path.casefold()] = (name, e.path)
    return table


# ------------------------------------------------------------- 树条目白名单

def check_entries(git: Git, entries: list[Entry], v: Verdict, *, threshold: int) -> None:
    """git 里只允许两种形态,其余一律拒绝。

    这是 blob 机制反过来读的结果,也是唯一能拦住"裸二进制从跳过 pre-commit 的
    路径进来"的东西——`--no-verify`、`cherry-pick`、`rebase` 都不跑 pre-commit。
    """
    for e in entries:
        if e.path == blob.BLOB_DIR or e.path.startswith(blob.BLOB_DIR + "/"):
            v.fail(f"{e.path} 在 blob/ 里 —— 那是存储,不该被跟踪")
        elif e.mode in (blob.MODE_FILE, blob.MODE_EXEC):
            head = git.cat_head(e.oid, blob.SNIFF)
            if blob.is_binary(head, size=git.size(e.oid), threshold=threshold):
                v.fail(f"{e.path} 是二进制却直接进了 git —— 应该先转成 blob 软链")
        elif e.mode == blob.MODE_LINK:
            target = git.cat(e.oid).decode(errors="replace")
            if not blob.points_into_store(e.path, target):
                v.fail(f"{e.path} 的软链没有指向 blob/ 之内 → {target}")
        elif e.mode == blob.MODE_SUBMODULE:
            v.fail(f"{e.path} 是 submodule(160000),不支持")
        else:
            v.fail(f"{e.path} 的模式 {e.mode} 不在白名单里")


def check_payload(
    git: Git,
    layers: L.Layers,
    tag: str | None,
    entries: list[Entry],
    paths: list[str],
    table: dict[str, tuple[str, str]],
    *,
    threshold: int = blob.DEFAULT_THRESHOLD,
) -> Verdict:
    """一条规则,`commit-msg`(快速反馈)和 ref 守卫(真正的保证)共用。
    两份实现迟早会不一致,而不一致的表现是仓库拒绝一切提交。"""
    v = Verdict()
    if tag is None:
        v.fail('提交信息必须以 [层名] 开头,例如:git commit -m "[%s] …"' % layers.top)
        return v
    if tag not in layers:
        v.fail(f"未知的层 [{tag}];当前 layers = {', '.join(layers.names)}")
        return v
    for p in paths:
        hit = table.get(p.casefold())
        if hit and hit[0] != tag:
            other, real = hit
            v.fail(f"{real} 属于层 [{other}],本次提交声明的是 [{tag}]")
    check_entries(git, entries, v, threshold=threshold)
    return v


# --------------------------------------------------------------- ref 更新

def check_update(git: Git, ref: str, old: str, new: str, *, threshold: int | None = None) -> Verdict:
    v = Verdict()
    if ref == "refs/heads/stack":
        return _check_stack(git, old, new, threshold)
    if not ref.startswith("refs/heads/layer/"):
        return v  # 别人的分支不归守卫管
    if new == ZERO:
        # 删除。`git pack-refs` 把 loose ref 迁进 packed-refs 就是这个形状,
        # 不放行的话 `git gc` 直接坏掉。
        return v
    if old == ZERO:
        return v  # 建分支(cb init / 加一层)

    layers = L.read_at(git, old) or L.read(git)
    if layers is None:
        return v
    name = ref.rsplit("/", 1)[1]
    if name not in layers:
        return v
    if threshold is None:
        threshold = _threshold(git, new)

    if not git.is_ancestor(old, new):
        v.fail(f"{ref[11:]} 不是 fast-forward —— rebase / reset / force 会改写已经落定的历史")
        return v
    if git.rev_list(f"{old}..{new}", "--merges"):
        v.fail(f"{ref[11:]} 的新增范围里有 merge 节点;权威分支必须是线性的")
        return v

    table = owners(git, layers)
    for commit in git.commits_between(old, new):
        tag = L.tag_of(git.subject(commit))
        if tag is not None and tag != name:
            v.fail(f"提交 {commit[:7]} 声明的是 [{tag}],却提交到了 {ref[11:]}")
            return v
        got = check_payload(
            git, layers, tag,
            git.changed_entries(commit),
            git.changed_paths(commit),
            table,
            threshold=threshold,
        )
        if not got.ok:
            v.errors += [f"提交 {commit[:7]}:{e}" for e in got.errors]
            return v
        for p in git.changed_paths(commit):
            table.setdefault(p.casefold(), (name, p))
    return v


def _check_stack(git: Git, old: str, new: str, threshold: int | None) -> Verdict:
    """stack 只接收 merge。

    规则很短:新增的每个提交都必须是 **merge 节点**,而且带合法的 ``[层名]``
    ——它的信息是从权威提交逐字复制来的,所以这条自然满足;不满足就说明那不是
    归位产生的。内容不用重查,merge 进来的那个提交在权威分支那一侧已经验过。

    不要求 fast-forward:``cb rebuild`` 会整条重造,它是构建产物。
    """
    v = Verdict()
    if new == ZERO or old == ZERO:
        return v
    layers = L.read_at(git, old) or L.read(git)
    if layers is None:
        return v
    if threshold is None:
        threshold = _threshold(git, new)
    for commit in git.commits_between(old, new):
        tag = L.tag_of(git.subject(commit))
        if tag is None:
            v.fail(f'提交 {commit[:7]} 的信息没有 [层名] 前缀:"{git.subject(commit)}"')
            return v
        if tag not in layers:
            # 层名按 old 判定,不是 new —— 否则一个提交可以同时加层并用这层
            # 给自己发证。
            v.fail(f"提交 {commit[:7]}:未知的层 [{tag}];当前 layers = {', '.join(layers.names)}")
            return v
        if len(git.parents(commit)) < 2:
            v.fail(
                f"提交 {commit[:7]} 不是 merge —— stack 只接收 merge,"
                "提交要打在 layer/<层> 上"
            )
            return v
        # merge 节点的内容已经在权威分支那一侧验过了,这里不重复。
    return v


def _threshold(git: Git, rev: str) -> int:
    import yaml

    try:
        raw = git.run("cat-file", "blob", f"{rev}:{L.CONFIG}")
    except Exception:
        return blob.DEFAULT_THRESHOLD
    try:
        cfg = yaml.safe_load(raw.decode()) or {}
        return _size(cfg.get("blob", {}).get("threshold")) or blob.DEFAULT_THRESHOLD
    except Exception:
        return blob.DEFAULT_THRESHOLD


def _size(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().upper()
    for suffix, mult in (("KB", 1 << 10), ("MB", 1 << 20), ("GB", 1 << 30), ("B", 1)):
        if text.endswith(suffix):
            try:
                return int(float(text[: -len(suffix)]) * mult)
            except ValueError:
                return None
    try:
        return int(text)
    except ValueError:
        return None
