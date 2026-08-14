#!/bin/bash
# 实验 3:单写入面 + [层名] 声明 + 投影。验证:全程无 merge 节点、所有分支 FF 安全、不变量成立。
set -u
R=/tmp/claude-1000/-home-twwyzh-collectbase/b57f764e-ac83-4eb8-a1cf-8155af4ae107/scratchpad/exp3
rm -rf "$R"; mkdir -p "$R"; cd "$R" || exit 1
export GIT_AUTHOR_NAME=cb GIT_AUTHOR_EMAIL=cb@x GIT_COMMITTER_NAME=cb GIT_COMMITTER_EMAIL=cb@x
git init -q .
sec() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

LAYERS="facts notes beliefs"          # 自下而上
FACE=stack/beliefs                    # 唯一写入面 = 最长那条
EMPTY=$(git hash-object -t tree /dev/null)

# ---------------------------------------------------------------- commit-msg hook
cat > .git/hooks/commit-msg <<'HOOK'
#!/bin/bash
msg=$(head -1 "$1")
layer=$(printf '%s' "$msg" | sed -n 's/^\[\([a-z][a-z0-9_-]*\)\].*/\1/p')
[ -z "$layer" ] && { echo "拒绝:提交信息必须以 [层名] 开头" >&2; exit 1; }
grep -qx "$layer" .git/cb-layers || { echo "拒绝:未知的层 '$layer'" >&2; exit 1; }
# 声明的层 vs 实际改动的路径
fail=0
while IFS= read -r p; do
  [ -z "$p" ] && continue
  owner=""
  while read -r L; do
    git cat-file -e "refs/heads/layer/$L:$p" 2>/dev/null && { owner=$L; break; }
  done < .git/cb-layers
  if [ -n "$owner" ] && [ "$owner" != "$layer" ]; then
    echo "拒绝:'$p' 属于层 [$owner],本次提交声明的是 [$layer]" >&2; fail=1
  fi
done < <(git diff --cached --name-only)
exit $fail
HOOK
chmod +x .git/hooks/commit-msg
printf 'facts\nnotes\nbeliefs\n' > .git/cb-layers

# ---------------------------------------------------------------- 引导:全部空树根提交
boot() { git update-ref "refs/heads/$1" "$(git commit-tree $EMPTY -m "[cb] init $1")"; }
boot "$FACE"; boot stack/notes
for L in $LAYERS; do boot "layer/$L"; done
git symbolic-ref HEAD "refs/heads/$FACE"; git reset -q --hard

# ---------------------------------------------------------------- 投影器(post-commit 干的事)
tree_of_union() {                       # 参数=层名列表 → 并集 tree oid
  local idx; idx=$(mktemp -u)
  for L in "$@"; do git ls-tree -r "refs/heads/layer/$L" | GIT_INDEX_FILE=$idx git update-index --index-info; done
  GIT_INDEX_FILE=$idx git write-tree; rm -f "$idx"
}
sync_down() {
  local S; S=$(git rev-parse "$FACE")
  local msg layer
  msg=$(git log -1 --format=%B "$S"); layer=$(printf '%s' "$msg" | sed -n 's/^\[\([a-z][a-z0-9_-]*\)\].*/\1/p')
  [ "$layer" = cb ] && return 0

  # 1) 投影到 layer/<声明层>:把 face 的树过滤成本层拥有的路径
  local own idx tree_k parent new
  own=$(mktemp); : > "$own"
  git ls-tree -r --name-only "refs/heads/layer/$layer" >> "$own"
  # 本次新增且不属于任何已知层的路径 → 归声明层
  git diff --name-only "$S^" "$S" 2>/dev/null | while IFS= read -r p; do
    found=0; for L in $LAYERS; do git cat-file -e "refs/heads/layer/$L:$p" 2>/dev/null && found=1; done
    [ $found -eq 0 ] && echo "$p"
  done >> "$own"
  sort -u "$own" -o "$own"

  idx=$(mktemp -u)
  git ls-tree -r "$S" | while IFS= read -r line; do
    p=${line#*$'\t'}; grep -qxF "$p" "$own" && echo "$line"
  done | GIT_INDEX_FILE=$idx git update-index --index-info
  tree_k=$(GIT_INDEX_FILE=$idx git write-tree); rm -f "$idx" "$own"

  parent=$(git rev-parse "refs/heads/layer/$layer")
  if [ "$(git rev-parse "$parent^{tree}")" != "$tree_k" ]; then
    new=$(printf '%s\n\nCb-Stack: %s\n' "$msg" "$S" | git commit-tree "$tree_k" -p "$parent")
    git update-ref "refs/heads/layer/$layer" "$new"
  fi

  # 2) 重算所有"包含声明层"的更短 stack(单亲,不产生 merge 节点)
  local acc=""
  for L in $LAYERS; do
    acc="$acc $L"
    [ "$L" = beliefs ] && continue          # 最长那条就是写入面本身
    [ "$L" = facts ] && continue            # 最底层不需要 stack:stack/facts ≡ layer/facts
    case " $acc " in *" $layer "*) ;; *) continue ;; esac
    local st=stack/$L t p2
    t=$(tree_of_union $acc); p2=$(git rev-parse "refs/heads/$st")
    if [ "$(git rev-parse "$p2^{tree}")" != "$t" ]; then
      git update-ref "refs/heads/$st" "$(printf '%s' "$msg" | git commit-tree "$t" -p "$p2")"
    fi
  done
}

# ---------------------------------------------------------------- 记录 tip,用于 FF 校验
BRANCHES="$FACE stack/notes layer/facts layer/notes layer/beliefs"
snapshot() { for b in $BRANCHES; do echo "$b $(git rev-parse "refs/heads/$b")"; done; }
PREV=$(snapshot)
ffcheck() {
  local now; now=$(snapshot); local bad=0
  while read -r b old; do
    new=$(echo "$now" | awk -v B="$b" '$1==B{print $2}')
    git merge-base --is-ancestor "$old" "$new" || { echo "    ✗ $b 非 FF"; bad=1; }
  done <<< "$PREV"
  [ $bad -eq 0 ] && echo "    ✓ 所有分支 fast-forward"
  PREV=$now
}

do_commit() { # $1=message  其余=文件
  local m=$1; shift
  for f in "$@"; do mkdir -p "$(dirname "$f")"; echo "rev $(git rev-list --count HEAD) of $f" > "$f"; done
  git add -A
  if git commit -q -m "$m" 2>&1 | sed 's/^/    /'; then sync_down; else return 1; fi
}

sec "① [facts] 建立事实层"
do_commit "[facts] 采集 api 文档与 8-14 日志" project/api.md project/log/2026-08-14.jsonl && ffcheck
sec "② [notes] 在同一目录里贴着放"
do_commit "[notes] api 的调用约束笔记" project/api-notes.md project/log/summary.md && ffcheck
sec "③ [beliefs] 再叠一层"
do_commit "[beliefs] 这次故障的根因判断" project/api-belief.md && ffcheck
sec "④ [facts] 上层已存在后,事实层继续前进"
do_commit "[facts] 采集 8-15 日志" project/log/2026-08-15.jsonl && ffcheck
sec "⑤ [beliefs] 修正自己的结论"
do_commit "[beliefs] 推翻上一版根因" project/api-belief.md && ffcheck

sec "⑥ 违规:声明 [beliefs] 却改事实层文件"
echo tampered > project/api.md; git add -A
git commit -q -m "[beliefs] 顺手修一下事实" 2>&1 | sed 's/^/    /'
git checkout -q -- project/api.md; git reset -q
sec "⑦ 违规:没有层标签"
echo x >> project/api-belief.md; git add -A
git commit -q -m "随手改改" 2>&1 | sed 's/^/    /'
git checkout -q -- project/api-belief.md; git reset -q

sec "全局:有没有 merge 节点"
n=$(git rev-list --merges --all | wc -l)
echo "    git rev-list --merges --all  →  $n 个"
sec "每条分支的历史"
for b in $FACE stack/notes layer/facts layer/notes layer/beliefs; do
  echo "--- $b"; git log --oneline --graph "refs/heads/$b" | sed 's/^/    /'
done
sec "不变量 tree(stack/Lk) == union(layer/L1..Lk)"
for pair in "notes:facts notes" "beliefs:facts notes beliefs"; do
  k=${pair%%:*}; ls=${pair#*:}
  st=stack/$k; a=$(git rev-parse "refs/heads/$st^{tree}"); b=$(tree_of_union $ls)
  [ "$a" = "$b" ] && echo "    ✓ $st" || echo "    ✗ $st  $a != $b"
done
sec "各分支内容"
for b in $FACE stack/notes layer/facts layer/notes layer/beliefs; do
  printf '    %-16s %s\n' "$b" "$(git ls-tree -r --name-only "refs/heads/$b" | tr '\n' ' ')"
done
