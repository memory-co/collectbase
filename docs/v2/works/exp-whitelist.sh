#!/bin/bash
# 实验 11:守卫改成「树条目白名单」——只有两种形态能进 git,其余一律拒。
set -u
R=/tmp/claude-1000/-home-twwyzh-collectbase/b57f764e-ac83-4eb8-a1cf-8155af4ae107/scratchpad/exp11
rm -rf "$R"; mkdir -p "$R"; cd "$R" || exit 1
export GIT_AUTHOR_NAME=cb GIT_AUTHOR_EMAIL=cb@x GIT_COMMITTER_NAME=cb GIT_COMMITTER_EMAIL=cb@x
git init -q -b stack/top .; sec(){ printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

printf 'facts\nnotes\n' > layers; echo "blob/" > .gitignore
mkdir -p project blob/2026/07/23; echo base > project/a.md
git add -A; git commit -q -m "[facts] init"
git update-ref refs/heads/layer/facts HEAD

cat > .git/hooks/reference-transaction <<'HOOK'
#!/bin/bash
[ "$1" = prepared ] || exit 0
Z=0000000000000000000000000000000000000000
is_binary() {   # 和 pre-commit 的 blobify 必须用同一份判据:file 也能读 stdin
  [ "$(git cat-file blob "$1" 2>/dev/null | file -b --mime-encoding -)" = binary ]
}
while read -r old new ref; do
  case "$ref" in refs/heads/stack/*) ;; *) continue ;; esac
  [ "$old" = "$new" ] && continue; [ "$new" = "$Z" ] && continue; [ "$old" = "$Z" ] && continue
  git merge-base --is-ancestor "$old" "$new" 2>/dev/null || { echo "  ✗ 非 FF" >&2; exit 1; }
  for c in $(git rev-list --reverse "$old..$new"); do
    L=$(git log -1 --format=%s "$c" | sed -n 's/^\[\([a-z][a-z0-9_-]*\)\].*/\1/p')
    [ -z "$L" ] && { echo "  ✗ 无 [层名]" >&2; exit 1; }
    # ★ 白名单:本次改动的每个条目,只允许两种形态
    while read -r mode type oid path; do
      case "$mode" in
        100644|100755)
          if is_binary "$oid"; then
            echo "  ✗ $path 是二进制却直接进了 git —— 应该先转成 blob 软链" >&2; exit 1; fi ;;
        120000)
          tgt=$(git cat-file blob "$oid")
          case "$tgt" in /*) echo "  ✗ $path 是绝对路径软链 → $tgt" >&2; exit 1 ;; esac
          # 解析后必须落在 blob/ 内
          full=$(cd "$(dirname "$path")" 2>/dev/null && realpath -m --relative-to=. "$tgt" >/dev/null; \
                 python3 -c "import os,sys;print(os.path.normpath(os.path.join(os.path.dirname(sys.argv[1]),sys.argv[2])))" "$path" "$tgt")
          case "$full" in blob/*) ;; *) echo "  ✗ $path 的软链逃出 blob/ → $tgt" >&2; exit 1 ;; esac ;;
        160000)
          echo "  ✗ $path 是 submodule(160000),不支持" >&2; exit 1 ;;
        *) echo "  ✗ $path 的模式 $mode 不在白名单里" >&2; exit 1 ;;
      esac
    done < <(git diff-tree --no-commit-id -r "$c" | awk '$5!="D"{print $2, "blob", $4, $6}')
  done
done
exit 0
HOOK
chmod +x .git/hooks/reference-transaction

t(){ printf '\n  \033[1m%s\033[0m\n' "$1"; shift; "$@" >/dev/null 2>/tmp/e
     grep -E '✗|fatal' /tmp/e | sed 's/^/    /' | head -3
     printf '    写入面 = %s\n' "$(git log -1 --format=%s)"
     git reset -q --hard HEAD >/dev/null 2>&1; git clean -qfd 2>/dev/null; }

sec "① 纯文本(应放行)"
echo hello > project/b.md; git add -A
t "git commit -m '[facts] 纯文本'" git commit -m "[facts] 纯文本"

sec "② --no-verify 直接塞一个裸二进制(pre-commit 被跳过,不会 blobify)"
head -c 2000000 /dev/urandom > project/raw.png; git add -A
t "git commit --no-verify -m '[facts] 裸二进制'" git commit --no-verify -m "[facts] 裸二进制"

sec "③ 合法的 blob 软链(应放行)"
head -c 1000 /dev/urandom > blob/2026/07/23/deadbeef.png
ln -sf ../blob/2026/07/23/deadbeef.png project/ok.png
git add -A
t "git commit -m '[facts] blob 软链'" git commit -m "[facts] blob 软链"

sec "④ 逃逸软链"
ln -s /etc/hostname project/leak.txt; git add -A
t "git commit --no-verify -m '[facts] 逃逸软链'" git commit --no-verify -m "[facts] 逃逸软链"

sec "⑤ 相对路径爬出仓库的软链"
ln -s ../../../etc/passwd project/leak2.txt; git add -A
t "git commit --no-verify -m '[facts] 爬出去'" git commit --no-verify -m "[facts] 爬出去"

sec "⑥ submodule(160000 条目)"
git update-index --add --cacheinfo 160000,$(git rev-parse HEAD),vendor/sub
t "git commit --no-verify -m '[facts] submodule'" git commit --no-verify -m "[facts] submodule"

sec "最终历史"
git log --oneline | sed 's/^/    /'
