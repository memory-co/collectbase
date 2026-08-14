#!/bin/bash
# 实验 10:自查。① 守卫会不会误伤 gc/pack-refs ② layers 从 new 读 = 自授权漏洞
#              ③ 逃逸软链 ④ git checkout -- <path> 会不会悄悄解锁下层文件
set -u
R=/tmp/claude-1000/-home-twwyzh-collectbase/b57f764e-ac83-4eb8-a1cf-8155af4ae107/scratchpad/exp10
rm -rf "$R"; mkdir -p "$R"; cd "$R" || exit 1
export GIT_AUTHOR_NAME=cb GIT_AUTHOR_EMAIL=cb@x GIT_COMMITTER_NAME=cb GIT_COMMITTER_EMAIL=cb@x
git init -q -b stack/top .; sec(){ printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

printf 'facts\nnotes\n' > layers; mkdir -p project; echo base > project/a.md
git add -A; git commit -q -m "[facts] collectbase: init"
git update-ref refs/heads/layer/facts HEAD
git update-ref refs/heads/layer/notes "$(git commit-tree $(git hash-object -t tree /dev/null) -m '[cb] init')"

# 守卫:layers 故意从 $new 读(= 我在实验 9 里写的那版)
cat > .git/hooks/reference-transaction <<'HOOK'
#!/bin/bash
[ "$1" = prepared ] || exit 0
while read -r old new ref; do
  case "$ref" in refs/heads/stack/*) ;; *) continue ;; esac
  [ "$old" = "$new" ] && continue
  case "$old" in 0000000000000000000000000000000000000000) continue ;; esac
  git merge-base --is-ancestor "$old" "$new" 2>/dev/null || { echo "  ✗ 非 FF" >&2; exit 1; }
  for c in $(git rev-list --reverse "$old..$new"); do
    L=$(git log -1 --format=%s "$c" | sed -n 's/^\[\([a-z][a-z0-9_-]*\)\].*/\1/p')
    [ -z "$L" ] && { echo "  ✗ 无 [层名]" >&2; exit 1; }
    git show "$new:layers" 2>/dev/null | grep -qx "$L" || { echo "  ✗ 未知层 [$L]" >&2; exit 1; }
  done
done
exit 0
HOOK
chmod +x .git/hooks/reference-transaction

sec "① 守卫会不会误伤 git gc / pack-refs"
echo x > project/b.md; git add -A; git commit -q -m "[facts] 一个提交"
git pack-refs --all 2>&1 | sed 's/^/    pack-refs: /'
echo "    pack-refs 退出码 $?  ;  .git/packed-refs 存在? $([ -f .git/packed-refs ] && echo 是 || echo 否)"
git gc -q 2>&1 | sed 's/^/    gc: /'; echo "    gc 退出码 $?"
echo "    写入面仍在: $(git rev-parse --short HEAD)"

sec "② 自授权:同一个提交里既加新层、又用这个新层的标签"
printf 'facts\nnotes\nevil\n' > layers
echo pwned > project/evil.md; git add -A
git commit -m "[evil] 我自己给自己发的证" 2>&1 | grep -E '✗|master|top' | sed 's/^/    /'
echo "    写入面 = $(git rev-parse --short HEAD)  ($(git log -1 --format=%s))"
echo "    layers 现在是: $(git show HEAD:layers | tr '\n' ' ')"
git reset -q --hard HEAD 2>/dev/null || true

sec "②b 守卫改成从 old 读 layers 之后"
sed -i 's|git show "$new:layers"|git show "$old:layers"|' .git/hooks/reference-transaction
git checkout -q -- . 2>/dev/null; git clean -qfd 2>/dev/null
printf 'facts\nnotes\nevil\n' > layers; echo pwned > project/evil2.md; git add -A
git commit -m "[evil] 再试一次" 2>&1 | grep -E '✗|fatal' | sed 's/^/    /'
echo "    写入面 = $(git rev-parse --short HEAD)  ($(git log -1 --format=%s))"
git checkout -q -- . ; git clean -qfd

sec "③ 逃逸软链:上层提交一个指向仓库外的软链"
ln -s /etc/hostname project/leak.txt
git add -A
git commit -q -m "[notes] 一个软链" 2>&1 | grep -E '✗|fatal' | sed 's/^/    /'
echo "    进去了吗: $(git ls-tree HEAD project/leak.txt)"
echo "    → 软链目标: $(git cat-file -p HEAD:project/leak.txt 2>/dev/null)"
echo "    (blob 机制假定 120000 都指向 blob/,这里指向仓库外)"

sec "④ git checkout -- <path> 会不会悄悄解锁下层文件"
chmod a-w project/a.md; echo "    加锁后: $(ls -l project/a.md | cut -c1-10)"
git checkout -- project/a.md 2>/dev/null
echo "    git checkout -- 之后: $(ls -l project/a.md | cut -c1-10)   (无 post-checkout 钩子触发)"
printf 'x' >> project/a.md 2>/dev/null && echo "    → 现在能写进去了" || echo "    → 仍然写不进"
