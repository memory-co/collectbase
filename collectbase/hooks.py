"""五个 git hook。

它们很薄:规则全在库里,因为 server 走 plumbing 时一个 hook 都不触发
(docs/v2/works/server.md)。两份实现迟早不一致,而不一致的表现是仓库拒绝
一切提交。

hook 看不懂这个仓库时就让开——没初始化、HEAD 游离、不是受管分支,一律
"不关我事",绝不是"拒绝"。
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import blob, layers as L, normalize, validate
from .gitrepo import Entry, Git

HINT_TAG = """
  提交信息要以 [层名] 开头,声明这次改动属于哪一层:
      git commit -m "[{top}] …"
  当前可用的层:{names}
"""

HINT_CROSS = """
  事实层是只读的——这是设计,不是权限配置错误。
  想表达"这条记录有问题",在自己层里另写一份并引用它:
      git commit -m "[{top}] 对 {path} 的存疑"

  本次提交未产生任何改动,工作区保持原样。
"""


def _repo() -> Git:
    return Git(Path.cwd())


def _here(git: Git, layers: L.Layers) -> str | None:
    """当前所在的位置:层名、``stack``、或 None(不归我们管)。"""
    head = git.head_ref()
    if head is None:
        return None
    if head == layers.stack_ref:
        return "stack"
    if head.startswith("refs/heads/layer/"):
        name = head.rsplit("/", 1)[1]
        return name if name in layers else None
    return None


# ------------------------------------------------------------------ 入口

def pre_commit(argv: list[str] | None = None) -> int:
    """把二进制转成 blob 软链,趁提交对象还没建起来。"""
    git = _repo()
    layers = L.read(git)
    if layers is None or _here(git, layers) is None:
        return 0

    # `--diff-filter` 必须含 T:替换一个媒体文件是把软链换回普通文件,
    # git 记的是类型变更,不是 M。
    staged = _lines(git.text("diff", "--cached", "--name-only", "--diff-filter=ACMRT", check=False))
    # `git commit -a` 的暂存发生在本 hook **之后**,所以要改的是工作区,
    # 只改索引会被随后的暂存覆盖。
    unstaged = _lines(git.text("diff", "--name-only", "--diff-filter=ACMRT", check=False))
    converted = blob.blobify_worktree(git, sorted(set(staged) | set(unstaged)))
    if converted:
        with (git.git_dir / "cb-converted").open("a") as fh:
            for c in converted:
                fh.write(f"{c.path}\t{c.orphan or ''}\n")
        for c in converted:
            _say(f"collectbase: {c.path} → {c.blob_path}")
    return 0


def commit_msg(argv: list[str]) -> int:
    """快速、好读的拒绝。保证由 ref 守卫给,这一道只是让失败来得更早。"""
    git = _repo()
    layers = L.read(git)
    if layers is None:
        return 0
    here = _here(git, layers)
    if here is None:
        return 0  # 别人的分支,不关我们的事

    subject = ""
    if argv:
        text = Path(argv[0]).read_text().splitlines()
        subject = text[0] if text else ""
    tag = L.tag_of(subject)

    if here != "stack" and tag is not None and tag != here:
        _say(
            f"✗ 拒绝提交:你在 layer/{here} 上,却声明了 [{tag}]。",
            f"  要么把信息改成 [{here}],要么切到 layer/{tag},",
            "  要么在 stack 上提交——那里所有层的文件都看得见,hook 会自动归位。",
        )
        return 1

    table = validate.owners(git, layers)
    paths = _lines(git.text("diff", "--cached", "--name-only", check=False))
    verdict = validate.check_payload(git, layers, tag, _staged_entries(git), paths, table)
    if verdict.ok:
        return 0

    _say("✗ 拒绝提交。", "")
    for e in verdict.errors:
        _say(f"  {e}")
    if tag is None:
        _say(HINT_TAG.format(top=layers.top, names="、".join(layers.names)))
    else:
        bad = next((p for p in paths if table.get(p.casefold())), "<path>")
        _say(HINT_CROSS.format(top=layers.top, path=bad))
    return 1


def post_commit(argv: list[str] | None = None) -> int:
    """归位:把提交放到权威层,再把 merge 节点做出来。"""
    git = _repo()
    layers = L.read(git)
    if layers is None:
        return 0

    _finish_blobs(git)

    try:
        notes = normalize.reconcile(git, layers)
    except normalize.NormalizeError as exc:
        _say(f"✗ collectbase:{exc}")
        return 1
    for n in notes:
        _say(f"collectbase: {n}")

    result = normalize.run(git, layers)
    if not result.ok:
        _say(
            "✗ collectbase:无法归位。",
            f"  {result.reason}",
            "  提交还在,但它没有落到任何权威层上。改完提交信息再试:",
            "      git commit --amend",
        )
        return 1
    _relock(git, layers)
    return 0


def post_checkout(argv: list[str] | None = None) -> int:
    """事实层只读。git 每次改写文件都会重置权限,所以每次 checkout 都要重打。"""
    git = _repo()
    layers = L.read(git)
    if layers is None:
        return 0
    _relock(git, layers)
    return 0


def reference_transaction(argv: list[str]) -> int:
    """真正的闸。其余都是体验。"""
    if not argv or argv[0] != "prepared":
        return 0
    git = _repo()
    failed = False
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        old, new, ref = parts
        verdict = validate.check_update(git, ref, old, new)
        if not verdict.ok:
            failed = True
            _say(f"✗ collectbase 拒绝更新 {ref}:")
            for e in verdict.errors:
                _say(f"    {e}")
    return 1 if failed else 0


# ------------------------------------------------------------------ 辅助

def _finish_blobs(git: Git) -> None:
    """部分提交时 pre-commit 只改得到临时索引,主索引还留着转换前的条目。"""
    state = git.git_dir / "cb-converted"
    if not state.exists():
        return
    paths, orphans = [], []
    for line in state.read_text().splitlines():
        p, _, o = line.partition("\t")
        paths.append(p)
        orphans.append(o)
    state.unlink()
    for p in paths:
        if (git.root / p).exists():
            git.run("add", "--", p, check=False)
    blob.drop_orphans(git, orphans)


def _relock(git: Git, layers: L.Layers) -> None:
    """把事实层的文件设成只读。

    只锁最底层:地板要守的是事实,中间层不是信任边界。归属检查在提交时照样
    执行,所以这一道只是最早的信号,不是保证。机制文件除外——它们在最底层里,
    但那是机制不是证据,锁上就没人能加层了。
    """
    here = _here(git, layers)
    if here is None:
        return
    head = git.resolve("HEAD")
    bottom = git.resolve(layers.layer_ref(layers.bottom))
    if head is None or bottom is None:
        return
    present = [e.path for e in git.ls_tree(head)]
    if here == layers.bottom:
        blob.unlock(git, present)  # 站在事实层上,就是来写事实的
        return
    floor = {e.path for e in git.ls_tree(bottom)} - L.META_PATHS
    blob.unlock(git, [p for p in present if p not in floor])
    blob.relock(git, [p for p in present if p in floor])


def _staged_entries(git: Git) -> list[Entry]:
    out = git.run("diff", "--cached", "-z", "--raw", "--no-renames", check=False)
    fields = [f for f in out.split(b"\0") if f]
    entries: list[Entry] = []
    i = 0
    while i < len(fields):
        meta = fields[i].decode()
        if not meta.startswith(":"):
            i += 1
            continue
        _src_mode, dst_mode, _src, dst, status = meta[1:].split()
        path = fields[i + 1].decode("utf-8", "surrogateescape")
        if status[0] != "D":
            entries.append(Entry(dst_mode, "blob", dst, path))
        i += 2
    return entries


def _lines(text: str) -> list[str]:
    return [x for x in text.splitlines() if x]


def _say(*lines: str) -> None:
    for line in lines:
        print(line, file=sys.stderr)


ENTRIES = {
    "pre-commit": pre_commit,
    "commit-msg": commit_msg,
    "post-commit": post_commit,
    "post-checkout": post_checkout,
    "reference-transaction": reference_transaction,
}
