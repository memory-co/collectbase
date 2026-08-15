"""``cb`` — two commands.

Everything readable is already git's (`git status`, `git log`,
`git branch --list 'layer/*'`; even "which layer owns this path" needs no
command, since lower layers are 444 and `ls -l` answers it). Everything
writable happens in a hook. What is left is what git cannot do: set the
repository up, and look inside the blob store.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import blob, check as check_mod, init as init_mod, layers as L
from .gitrepo import Git

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_USAGE = 2
EXIT_BROKEN = 3
EXIT_UNINITIALISED = 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cb", description="分层记录文件系统")
    parser.add_argument("-C", dest="repo", default=".", help="仓库路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="在已有仓库上建立分层拓扑")
    p_init.add_argument("--layers", required=True, help="自下而上,逗号分隔,例如 facts,notes,beliefs")

    sub.add_parser("check", help="校验 I1–I4")
    sub.add_parser("rebuild", help="从各层 tip 重建 stack(它是构建产物)")

    p_blob = sub.add_parser("blob", help="blob 库维护")
    p_blob_sub = p_blob.add_subparsers(dest="blob_cmd", required=True)
    p_gc = p_blob_sub.add_parser("gc", help="删掉没有任何提交引用的 blob")
    p_gc.add_argument("-n", "--dry-run", action="store_true")

    args = parser.parse_args(argv)
    git = Git(Path(args.repo))

    if args.cmd == "init":
        return _init(git, args.layers)
    if args.cmd == "check":
        return _check(git)
    if args.cmd == "rebuild":
        return _rebuild(git)
    if args.cmd == "blob":
        return _blob_gc(git, args.dry_run)
    return EXIT_USAGE


def _init(git: Git, spec: str) -> int:
    names = [x.strip() for x in spec.split(",") if x.strip()]
    try:
        plan = init_mod.run(git, names)
    except (init_mod.InitError, L.LayersError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return EXIT_USAGE

    layers = L.Layers(tuple(names))
    if not plan.created and not plan.adopted:
        print("已初始化过。补上本地配置(core.hooksPath 不随 clone 传播),并切到写入面。")
        print("  合并视图  stack")
        return EXIT_OK

    print(f"始祖提交  {plan.start_point[:7]}  [{layers.bottom}] collectbase: init")
    print(f"既有 {plan.adopted} 个文件全部划归 [{layers.bottom}]")
    print()
    for name in layers.names:
        print(f"  layer/{name:<12} → {plan.start_point[:7]}")
    print(f"  {'stack':<18} → {plan.start_point[:7]}  ← 合并视图(已切过去)")
    print()
    print("  所有分支都指向始祖那一个提交。共同祖先在那儿,merge 的基就有了:")
    print("  各层此后各加各的文件,git 算并集时那些老文件是基,不会撞。")
    print()
    print(f"hook 已装,core.hooksPath = {git.text('config', 'core.hooksPath')}")
    print(f"提交时以 {' / '.join('[%s]' % n for n in layers.names)} 开头声明所属层。")
    print("在 stack 上提交(看得见所有层)或直接在 layer/<层> 上提交,hook 都会归位。")
    return EXIT_OK


def _check(git: Git) -> int:
    report = check_mod.run(git)
    if report.start_point:
        print(f"起始点位  {report.start_point[:7]}")
    for f in report.findings:
        print(f"  {f.id:<4} {'✓' if f.ok else '✗'}  {f.summary}")
        for line in f.details:
            print(f"           {line}")
        if f.remedy and not f.ok:
            for line in f.remedy.splitlines():
                print(f"           {line}")
    if not report.findings:
        return EXIT_OK
    if report.findings[0].id == "init":
        return EXIT_UNINITIALISED
    return EXIT_OK if report.ok else EXIT_BROKEN


def _rebuild(git: Git) -> int:
    from . import normalize

    try:
        new = normalize.rebuild(git)
    except normalize.NormalizeError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return EXIT_BROKEN
    print(f"stack → {new[:7]}(各层 tip 的并集)")
    return EXIT_OK


def _blob_gc(git: Git, dry_run: bool) -> int:
    dead = blob.gc(git, dry_run=dry_run)
    if not dead:
        print("没有可删的 blob(活集覆盖了库里的全部文件)")
        return EXIT_OK
    for p in dead:
        print(("会删  " if dry_run else "已删  ") + p)
    print(f"{'可删' if dry_run else '已删'} {len(dead)} 个")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
