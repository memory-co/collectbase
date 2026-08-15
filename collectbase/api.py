"""路径上的增删改查。

server 对外就是这个;HTTP 和网页只是它的调用方。它**不实现任何规则**——写入
走的是和敲 `git commit` 完全相同的那条路(落在 ``layer/<L>`` 上,再 merge 进
``stack``),规则由守卫说了算,这里只负责把守卫的话原样转出去。

**呈现文件,不呈现 git 对象。**读一个 ``120000`` 条目返回的是字节;写二进制
由这里转 blob。调用方不知道 blob 机制存在。

见 docs/v2/works/server.md §1。
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path

from . import blob, layers as L, normalize, validate
from .gitrepo import Entry, Git, GitError


class ApiError(RuntimeError):
    """带 HTTP 状态码的错误。文字直接给用户看,所以要说人话。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ 读

def _ref_for(git: Git, layers: L.Layers, view: str | None) -> str:
    """``view`` 是**读哪一层的视图**;写入时声明的层是另一回事,别混。"""
    if view in (None, "", "all", "stack"):
        return layers.stack_ref
    if view not in layers:
        raise ApiError(f"没有这一层:{view};当前 layers = {', '.join(layers.names)}", 404)
    return layers.layer_ref(view)


def read(git: Git, path: str, view: str | None = None) -> bytes:
    layers = _layers(git)
    ref = _ref_for(git, layers, view)
    entry = _entry(git, ref, path)
    if entry is None:
        raise ApiError(f"没有这个文件:{path}", 404)
    if entry.mode != blob.MODE_LINK:
        return git.cat(entry.oid)

    target = git.cat(entry.oid).decode(errors="replace")
    resolved = blob.resolve_target(path, target)
    if resolved is None:
        raise ApiError(f"{path} 的软链指向仓库之外:{target}", 500)
    local = git.root / resolved
    if not local.is_file():
        raise ApiError(
            f"{path} 的内容不在本地(blob 库缺这一份)。拉回来:cb blob pull <url>", 503
        )
    return local.read_bytes()


@dataclass
class Item:
    name: str
    path: str
    is_dir: bool
    size: int
    layer: str | None
    modified: int | None
    is_blob: bool


def listdir(git: Git, path: str = "", view: str | None = None) -> list[Item]:
    layers = _layers(git)
    ref = _ref_for(git, layers, view)
    prefix = path.strip("/")
    prefix = prefix + "/" if prefix else ""
    table = validate.owners(git, layers)

    dirs: set[str] = set()
    items: list[Item] = []
    for e in git.ls_tree(ref):
        if not e.path.startswith(prefix):
            continue
        rest = e.path[len(prefix):]
        if "/" in rest:
            dirs.add(rest.split("/", 1)[0])
            continue
        owner = table.get(e.path.casefold())
        items.append(Item(
            name=rest,
            path=e.path,
            is_dir=False,
            size=_size_of(git, e),
            layer=owner[0] if owner else None,
            modified=_modified(git, ref, e.path),
            is_blob=e.mode == blob.MODE_LINK,
        ))
    items += [
        Item(name=d, path=prefix + d, is_dir=True, size=0, layer=None,
             modified=None, is_blob=False)
        for d in sorted(dirs)
    ]
    items.sort(key=lambda i: (not i.is_dir, i.name))
    return items


def _size_of(git: Git, e: Entry) -> int:
    """blob 条目要报**真实文件**的大小,不是那条软链的 90 字节。"""
    if e.mode != blob.MODE_LINK:
        return git.size(e.oid)
    target = git.cat(e.oid).decode(errors="replace")
    resolved = blob.resolve_target(e.path, target)
    local = git.root / resolved if resolved else None
    return local.stat().st_size if local and local.is_file() else 0


def _modified(git: Git, ref: str, path: str) -> int | None:
    out = git.text("log", "-1", "--format=%at", ref, "--", path, check=False)
    return int(out) if out.isdigit() else None


def _entry(git: Git, ref: str, path: str) -> Entry | None:
    got = git.ls_tree(ref, path)
    return got[0] if got and got[0].path == path else None


# ------------------------------------------------------------------ 写

@dataclass
class Put:
    path: str
    content: bytes


@dataclass
class Delete:
    path: str


def commit(git: Git, layer: str, message: str, ops: list[Put | Delete]) -> str:
    """一批 ops → **一个**提交,落在 ``layer/<layer>`` 上,再 merge 进 stack。

    提交是变更的单位:拖进 5 个文件是一次动作,改名是"新增 + 删除"同时发生,
    都该是一个提交。
    """
    layers = _layers(git)
    if layer not in layers:
        raise ApiError(f"没有这一层:{layer};当前 layers = {', '.join(layers.names)}", 400)
    if not ops:
        raise ApiError("没有任何改动", 400)

    subject = message.strip().splitlines()[0] if message.strip() else ""
    if L.tag_of(subject) != layer:
        message = f"[{layer}] {message.strip() or '更新 %d 个路径' % len(ops)}"

    ref = layers.layer_ref(layer)
    tip = git.resolve(ref)
    if tip is None:
        raise ApiError(f"{ref} 不存在,仓库还没初始化好", 500)

    entries = {e.path: e for e in git.ls_tree(ref)}
    touched: list[str] = []
    for op in ops:
        path = op.path.strip("/")
        if not path or path.startswith("../") or "/../" in path:
            raise ApiError(f"路径不合法:{op.path}", 400)
        touched.append(path)
        if isinstance(op, Delete):
            if path not in entries:
                raise ApiError(f"没有这个文件:{path}", 404)
            entries.pop(path)
        else:
            entries[path] = _entry_for(git, path, op.content)

    tree = git.write_tree(list(entries.values()))
    if tree == git.text("rev-parse", f"{tip}^{{tree}}"):
        raise ApiError("这批改动没有产生任何变化", 400)

    new = git.commit_tree(tree, [tip], message)
    try:
        git.update_ref(ref, new, old=tip, reason="collectbase: api")
    except GitError as exc:
        # 守卫拒了。原样转出去 —— 页面上显示的字就是命令行里会看到的字。
        raise ApiError(_reason(exc), 409) from None

    # 这次提交可能动了锚定 —— 分支集合要先跟上,和 post-commit 走同一条路。
    try:
        layers = L.read(git) or layers
        normalize.reconcile(git, layers)
    except normalize.NormalizeError as exc:
        raise ApiError(str(exc), 409) from None

    result = normalize.run(git, layers)
    if not result.ok:
        raise ApiError(f"改动已落在 {ref[11:]},但 merge 进 stack 失败:{result.reason}", 500)
    _sync_worktree(git, ref, touched)
    _sync_worktree(git, layers.stack_ref, touched)
    return new


def _entry_for(git: Git, path: str, content: bytes) -> Entry:
    """二进制转 blob 软链 —— 和 `pre-commit` 用的是同一份判据和同一个转换器。

    server 走 plumbing,一个 hook 都不触发,所以这一步得自己做;但**必须**和
    hook 共用实现,否则"转换器说是文本、守卫说是二进制"会让提交永远进不去。
    """
    if blob.is_binary(content):
        return blob.store_symlink(git, path, content)
    return Entry(blob.MODE_FILE, "blob", git.hash_object(content), path)


def _sync_worktree(git: Git, ref: str, paths: list[str]) -> None:
    """有工作区正 checkout 在这条分支上的话,把这几个精确路径补过去。

    ``update-ref`` 之后它的 HEAD 动了、磁盘没动,`git status` 会把新文件显示
    成已删除。只碰这次涉及的路径,不碰别人没提交完的东西。
    """
    if git.head_ref() != ref:
        return
    tip = git.resolve(ref)
    if tip is None:
        return
    present = {e.path: e for e in git.ls_tree(tip)}
    for path in paths:
        entry = present.get(path)
        if entry is None:
            git.run("update-index", "--force-remove", "--", path, check=False)
            (git.root / path).unlink(missing_ok=True)
        else:
            git.run("update-index", "--add", "--cacheinfo",
                    f"{entry.mode},{entry.oid},{path}", check=False)
            git.run("checkout-index", "-f", "--", path, check=False)


def _reason(exc: GitError) -> str:
    lines = [x.strip() for x in exc.stderr.splitlines() if x.strip()]
    body = [x for x in lines if not x.startswith("fatal:")]
    return "\n".join(body or lines) or str(exc)


def _layers(git: Git) -> L.Layers:
    got = L.read(git)
    if got is None:
        raise ApiError("这个仓库还没初始化:cb init --layers …", 404)
    return got


def info(git: Git) -> dict:
    layers = _layers(git)
    return {
        "layers": list(layers.names),
        "bottom": layers.bottom,
        "start_point": L.start_point(git),
        "blob": {"live": len(blob.live_set(git))},
    }


def _unused() -> Path:  # pragma: no cover
    return Path(posixpath.sep)
