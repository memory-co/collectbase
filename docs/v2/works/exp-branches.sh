#!/bin/bash
# 实验 8:别的分支能不能把内容捅进写入面?哪些操作绕过 commit-msg?ref 更新能否被否决?
set -u
R=/tmp/claude-1000/-home-twwyzh-collectbase/b57f764e-ac83-4eb8-a1cf-8155af4ae107/scratchpad/exp8
rm -rf "$R"; mkdir -p "$R"; cd "$R" || exit 1
export GIT_AUTHOR_NAME=cb GIT_AUTHOR_EMAIL=cb@x GIT_COMMITTER_NAME=cb GIT_COMMITTER_EMAIL=cb@x
git init -q -b stack/top .; sec() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

for h in pre-commit commit-msg post-commit pre-merge-commit post-merge post-rewrite post-checkout; do
  printf '#!/bin/sh\necho "      [hook] %s" >&2\nexit 0\n' "$h" > .git/hooks/$h
  chmod +x .git/hooks/$h
done

echo base > a.txt; git add -A; git commit -q -m "[facts] base" 2>/dev/null
git branch other
BASE=$(git rev-parse HEAD)

git checkout -q other
echo x > evil.txt; git add -A; git commit -q -m "没有层标签的提交" 2>/dev/null
echo y >> evil.txt; git add -A; git commit -q -m "又一个" 2>/dev/null
OTHER=$(git rev-parse HEAD)
git checkout -q -b clean $BASE; echo cp > cp.txt; git add -A; git commit -q -m "无标签的可摘提交" 2>/dev/null; CLEAN=$(git rev-parse HEAD)
git checkout -q stack/top

run() { printf '\n  \033[1m%s\033[0m\n' "$1"; shift; "$@" >/dev/null 2>/tmp/e; sed 's/^/      /' /tmp/e | grep -v '^\s*$' | head -6; }
face() { echo "      → 写入面 = $(git rev-parse --short HEAD)  ($(git log -1 --format=%s))"; }

sec "① git merge other(可 fast-forward)"
run "git merge other" git merge other; face
git reset -q --hard $BASE

sec "② git merge --no-ff other"
run "git merge --no-ff -m merge other" git merge --no-ff -m merge other; face
echo "      merge 节点数: $(git rev-list --merges HEAD | wc -l)"
git reset -q --hard $BASE

sec "③ git cherry-pick(从 other 摘一个过来)"
run "git cherry-pick $CLEAN" git cherry-pick $CLEAN; face
git reset -q --hard $BASE

sec "④ git reset --hard other"
run "git reset --hard other" git reset --hard other; face
git reset -q --hard $BASE

sec "⑤ git rebase other"
echo z > b.txt; git add -A; git commit -q -m "[facts] 本地一个提交" 2>/dev/null
run "git rebase other" git rebase other; face
git rebase --abort 2>/dev/null; git reset -q --hard $BASE

sec "⑥ 装 reference-transaction:能否否决对 stack/* 的非法 ref 更新"
cat > .git/hooks/reference-transaction <<'HOOK'
#!/bin/bash
[ "$1" = prepared ] || exit 0
while read -r old new ref; do
  case "$ref" in refs/heads/stack/*|refs/heads/layer/*) ;; *) continue ;; esac
  [ -f "$(git rev-parse --git-dir)/cb-allow" ] && continue
  echo "      [ref-txn] 拒绝更新 $ref —— 未经 collectbase" >&2
  exit 1
done
exit 0
HOOK
chmod +x .git/hooks/reference-transaction
printf '#!/bin/sh\necho "      [hook] commit-msg 校验通过,放令牌" >&2\n: > "$(git rev-parse --git-dir)/cb-allow"\nexit 0\n' > .git/hooks/commit-msg
printf '#!/bin/sh\necho "      [hook] post-commit 收令牌" >&2\nrm -f "$(git rev-parse --git-dir)/cb-allow"\nexit 0\n' > .git/hooks/post-commit

run "git reset --hard other   (应被拒)" git reset --hard other; face
run "git merge other          (应被拒)" git merge other; face
echo c > c.txt; git add -A
run "git commit -m '[facts] 正常提交'  (应放行)" git commit -m "[facts] 正常提交"; face
echo
echo "  最终:写入面历史"
git log --oneline | sed 's/^/      /'
