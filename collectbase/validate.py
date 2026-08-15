"""The guard: check what lands, not who put it there.

`commit-msg` only sees "someone is writing a new commit". Merge-ff, reset,
rebase and cherry-pick all move the write face without it firing — measured in
docs/v2/works/exp-refguard.sh. So the real enforcement point is
`reference-transaction`, and the rule is stated over the *content* of the
range being added, which makes it indifferent to the git command used.
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
    """casefold(path) -> (layer, real path).

    Case-folded because on a case-insensitive filesystem ``Facts/a.md`` and
    ``facts/a.md`` are two git paths but one file on disk.
    """
    table: dict[str, tuple[str, str]] = {}
    for name in layers:
        ref = layers.layer_ref(name)
        if git.resolve(ref) is None:
            continue
        for e in git.ls_tree(ref):
            table[e.path.casefold()] = (name, e.path)
    return table


# ------------------------------------------------------------- 树条目白名单

def check_entries(git: Git, entries: list[Entry], v: Verdict, *, threshold: int) -> None:
    """Only two shapes may enter git. Everything else is refused.

    This is the blob mechanism read backwards, and it is what stops a raw
    binary from arriving through a path that skips `pre-commit` — `--no-verify`,
    `cherry-pick` and `rebase` all do.
    """
    for e in entries:
        if e.path == blob.BLOB_DIR or e.path.startswith(blob.BLOB_DIR + "/"):
            v.fail(f"{e.path} 在 blob/ 里 —— 那是存储,不该被跟踪")
            continue
        if e.mode in (blob.MODE_FILE, blob.MODE_EXEC):
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


def check_ownership(
    tag: str, paths: list[str], table: dict[str, tuple[str, str]], v: Verdict
) -> None:
    for p in paths:
        hit = table.get(p.casefold())
        if hit and hit[0] != tag:
            other, real = hit
            v.fail(f"{real} 属于层 [{other}],本次提交声明的是 [{tag}]")


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
    """One rule, shared by `commit-msg` (fast feedback) and the ref guard
    (the actual guarantee). Two copies would drift apart."""
    v = Verdict()
    if tag is None:
        v.fail("提交信息必须以 [层名] 开头,例如:git commit -m \"[%s] …\"" % layers.top)
        return v
    if tag not in layers:
        v.fail(f"未知的层 [{tag}];当前 layers = {', '.join(layers.names)}")
        return v
    check_ownership(tag, paths, table, v)
    check_entries(git, entries, v, threshold=threshold)
    return v


# --------------------------------------------------------------- ref 更新

def check_update(git: Git, ref: str, old: str, new: str, *, threshold: int | None = None) -> Verdict:
    v = Verdict()
    if not (ref.startswith("refs/heads/layer/") or ref.startswith("refs/heads/stack/")):
        return v
    if new == ZERO:
        # Deletion. `git pack-refs` presents the loose->packed migration this
        # way; refusing it breaks `git gc` outright.
        return v
    if old == ZERO:
        return v  # branch creation (cb init)

    layers = L.read_at(git, old) or L.read(git)
    if layers is None:
        return v
    if threshold is None:
        threshold = _threshold(git, new)

    if ref == layers.write_face:
        _check_write_face(git, layers, old, new, v, threshold=threshold)
    elif ref in layers.managed_refs():
        _check_derived(git, layers, ref, old, new, v)
    return v


def _check_write_face(
    git: Git, layers: L.Layers, old: str, new: str, v: Verdict, *, threshold: int
) -> None:
    if not git.is_ancestor(old, new):
        v.fail("不是 fast-forward —— rebase / reset / force 会改写已经落定的历史")
        return
    if git.rev_list(f"{old}..{new}", "--merges"):
        v.fail("新增范围里有 merge 节点;这套设计不允许 merge")
        return

    cursor = git.resolve(L.PROJECTED_REF)
    if cursor is not None and cursor != old:
        v.fail(
            "写入面领先于投影,先把投影补齐再提交"
            f"(已投影到 {cursor[:7]},写入面在 {old[:7]})"
        )
        return

    table = owners(git, layers)
    for commit in git.commits_between(old, new):
        tag = L.tag_of(git.subject(commit))
        got = check_payload(
            git, layers, tag,
            git.changed_entries(commit),
            git.changed_paths(commit),
            table,
            threshold=threshold,
        )
        if not got.ok:
            short = commit[:7]
            v.errors += [f"提交 {short}:{e}" for e in got.errors]
            return
        # a commit may claim previously unowned paths for its own layer
        if tag:
            for p in git.changed_paths(commit):
                table.setdefault(p.casefold(), (tag, p))


def _check_derived(git: Git, layers: L.Layers, ref: str, old: str, new: str, v: Verdict) -> None:
    """Derived branches may only ever contain material already on the write
    face. Exact equality (I2) is `cb check`'s job; the guard's job is to stop
    anything being smuggled in through a generated branch."""
    if not git.is_ancestor(old, new):
        v.fail(f"{ref} 不是 fast-forward")
        return
    face = git.resolve(layers.write_face)
    if face is None:
        return
    have = {(e.path, e.mode, e.oid) for e in git.ls_tree(face)}
    for e in git.ls_tree(new):
        if (e.path, e.mode, e.oid) not in have:
            v.fail(f"{ref} 含有写入面上没有的内容:{e.path}(派生分支只能由投影更新)")
            return


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
