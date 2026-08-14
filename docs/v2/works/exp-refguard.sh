#!/bin/bash
# 实验 9:reference-transaction 里做「内容检查」而非「令牌」——检查 old..new 里每个提交是否合规。
set -u
R=/tmp/claude-1000/-home-twwyzh-collectbase/b57f764e-ac83-4eb8-a1cf-8155af4ae107/scratchpad/exp9
rm -rf "$R"; mkdir -p "$R"; cd "$R" || exit 1
export GIT_AUTHOR_NAME=cb GIT_AUTHOR_EMAIL=cb@x GIT_COMMITTER_NAME=cb GIT_COMMITTER_EMAIL=cb@x
git init -q -b stack/top .; sec(){ printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

printf 'facts\nnotes\n' > layers
mkdir -p project; echo base > project/a.md
git add -A; git commit -q -m "[facts] collectbase: init"
git update-ref refs/heads/layer/facts HEAD
START=$(git rev-parse HEAD)

# ---------------- 唯一的执行点:reference-transaction,纯内容检查,无令牌
cat > .git/hooks/reference-transaction <<'HOOK'
#!/bin/bash
[ "$1" = prepared ] || exit 0
GD=$(git rev-parse --git-dir)
while read -r old new ref; do
  [ "$ref" = refs/heads/stack/top ] || continue
  [ "$old" = "$new" ] && continue
  case "$old" in 0000000000000000000000000000000000000000) continue ;; esac   # 建分支

  # ① 必须 fast-forward
  if ! git merge-base --is-ancestor "$old" "$new" 2>/dev/null; then
    echo "  ✗ 拒绝:不是 fast-forward(rebase / reset / force 会改写已有历史)" >&2; exit 1
  fi
  # ② 新增范围里不能有 merge 节点
  if [ -n "$(git rev-list --merges "$old..$new")" ]; then
    echo "  ✗ 拒绝:新增范围里有 merge 节点" >&2; exit 1
  fi
  # ③ 每个新提交都要有合法 [层名],且改动路径归属该层
  for c in $(git rev-list --reverse "$old..$new"); do
    subj=$(git log -1 --format=%s "$c")
    L=$(printf '%s' "$subj" | sed -n 's/^\[\([a-z][a-z0-9_-]*\)\].*/\1/p')
    if [ -z "$L" ]; then
      echo "  ✗ 拒绝:提交 $(git rev-parse --short $c) 的信息没有 [层名] 前缀" >&2
      echo "     \"$subj\"" >&2; exit 1
    fi
    if ! git show "$new:layers" 2>/dev/null | grep -qx "$L"; then
      echo "  ✗ 拒绝:提交 $(git rev-parse --short $c) 声明了未知的层 [$L]" >&2; exit 1
    fi
    for p in $(git diff-tree --no-commit-id --name-only -r "$c"); do
      for other in $(git show "$new:layers" | grep -v "^$L$"); do
        if git cat-file -e "refs/heads/layer/$other:$p" 2>/dev/null; then
          echo "  ✗ 拒绝:$p 属于层 [$other],提交 $(git rev-parse --short $c) 声明的是 [$L]" >&2
          exit 1
        fi
      done
    done
  done
done
exit 0
HOOK
chmod +x .git/hooks/reference-transaction

git branch other $START
git checkout -q other
echo x > project/scratch.md; git add -A; git commit -q -m "随手写的,没有层标签"
UNTAGGED=$(git rev-parse HEAD)
echo y > project/tagged.md; git add -A; git commit -q -m "[notes] 一个合规的提交"
TAGGED=$(git rev-parse HEAD)
git checkout -q stack/top

clean(){ git merge --abort 2>/dev/null; git cherry-pick --abort 2>/dev/null; git rebase --abort 2>/dev/null
  rm -f .git/MERGE_HEAD .git/CHERRY_PICK_HEAD; rm -rf .git/sequencer; git reset -q --hard HEAD 2>/dev/null; }
run(){ clean; printf '\n  \033[1m%s\033[0m\n' "$1"; shift; "$@" >/dev/null 2>/tmp/e; grep -E '✗|fatal|error' /tmp/e | sed 's/^/    /' | head -4; printf '    写入面 = %s (%s)\n' "$(git rev-parse --short HEAD)" "$(git log -1 --format=%s)"; }

sec "合法路径"
echo ok > project/b.md; git add -A
run "git commit -m '[facts] 正常提交'" git commit -m "[facts] 正常提交"
NORMAL=$(git rev-parse HEAD)

sec "① --no-verify + 没有层标签"
echo z > project/c.md; git add -A
run "git commit --no-verify -m '偷偷提交'" git commit --no-verify -m "偷偷提交"
git checkout -q -- . 2>/dev/null; git reset -q

sec "② --no-verify + 改事实层的文件"
echo tampered > project/a.md; git add -A
run "git commit --no-verify -m '[notes] 顺手改事实'" git commit --no-verify -m "[notes] 顺手改事实"
git checkout -q -- project/a.md; git reset -q

sec "③ git merge other —— 真 fast-forward(先把写入面退回起始点位)"
clean; git reset -q --hard $START
run "git merge other  (FF,带一个无标签提交)" git merge other
git reset -q --hard $NORMAL

sec "④ git merge --no-ff other"
run "git merge --no-ff -m merge other" git merge --no-ff -m merge other

sec "⑤ git reset --hard other"
run "git reset --hard other" git reset --hard other

sec "⑥ git cherry-pick <无标签的提交>"
run "git cherry-pick $UNTAGGED" git cherry-pick $UNTAGGED

sec "⑦ git cherry-pick <合规的 [notes] 提交> —— 内容合规就该放行"
run "git cherry-pick $TAGGED" git cherry-pick $TAGGED

clean
sec "最终历史(应只含合规提交)"
git log --oneline | sed 's/^/    /'
