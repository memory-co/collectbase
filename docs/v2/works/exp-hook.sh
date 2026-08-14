#!/bin/bash
# 实验 6:改进版钩子 —— 改的是「工作区」而不只是索引,并用 post-commit 修主索引。
set -u
R=/tmp/claude-1000/-home-twwyzh-collectbase/b57f764e-ac83-4eb8-a1cf-8155af4ae107/scratchpad/exp6
rm -rf "$R"; mkdir -p "$R"; cd "$R" || exit 1
export GIT_AUTHOR_NAME=cb GIT_AUTHOR_EMAIL=cb@x GIT_COMMITTER_NAME=cb GIT_COMMITTER_EMAIL=cb@x
git init -q .; sec() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

cat > .git/hooks/pre-commit <<'HOOK'
#!/bin/bash
# 候选 = 已暂存的 ∪ 工作区已改的(覆盖 commit -a:钩子跑的时候 -a 还没暂存)
{ git diff --cached --name-only --diff-filter=ACMRT
  git diff        --name-only --diff-filter=ACM ; } | sort -u |
while IFS= read -r f; do
  [ -L "$f" ] && continue
  [ -f "$f" ] || continue
  [ "$(file -b --mime-encoding "$f")" = binary ] || continue
  sha=$(sha256sum "$f" | cut -c1-64); ext=${f##*.}; [ "$ext" = "$f" ] && ext=bin
  dst="blob/2026/07/23/$sha.$ext"; mkdir -p "$(dirname "$dst")"
  [ -e "$dst" ] || { cp "$f" "$dst"; chmod 444 "$dst"; }
  rm -f "$f"; ln -s "$(realpath --relative-to="$(dirname "$f")" "$dst")" "$f"
  git rm -q --cached "$f" >/dev/null 2>&1; git add "$f"          # 改当前索引(可能是临时索引)
  echo "$f" >> .git/cb-converted                                  # 留给 post-commit 修主索引
  echo "    [pre-commit] → 软链 $f"
done
exit 0
HOOK
cat > .git/hooks/post-commit <<'HOOK'
#!/bin/bash
# 部分提交时 pre-commit 只改得到临时索引,主索引还留着转换前的条目;这里补上
[ -f .git/cb-converted ] || exit 0
while IFS= read -r f; do [ -e "$f" ] && git add "$f" 2>/dev/null; done < .git/cb-converted
rm -f .git/cb-converted
HOOK
chmod +x .git/hooks/pre-commit .git/hooks/post-commit

echo "blob/" > .gitignore; git add .gitignore; git commit -qm init --no-verify

check() {
  local mode; mode=$(git ls-tree HEAD "$2" | awk '{print $1}')
  case "$mode" in
    120000) echo "    ✅ $1:提交里是软链,$(git cat-file -s "HEAD:$2") 字节" ;;
    "")     echo "    ❓ $1:该路径不在提交里" ;;
    *)      echo "    ❌ $1:提交里是 $mode,$(git cat-file -s "HEAD:$2") 字节" ;;
  esac
}

sec "① 普通 git commit"
mkdir -p a; head -c 2000000 /dev/urandom > a/x.png; git add a/x.png
git commit -qm normal; check "git commit" a/x.png

sec "② git commit -a"
rm a/x.png; head -c 2000000 /dev/urandom > a/x.png
git commit -qam "commit -a"; check "git commit -a" a/x.png

sec "③ 部分提交 git commit -- <path>"
mkdir -p b; head -c 2000000 /dev/urandom > b/y.png; head -c 2000000 /dev/urandom > b/z.png
git add b/y.png b/z.png
git commit -qm partial -- b/y.png; check "git commit -- path" b/y.png
echo "    提交后 b/y.png:工作区=$(ls -l b/y.png|cut -c1-1)  索引状态='$(git status --porcelain b/y.png)'"
echo "    提交后 b/z.png:工作区=$(ls -l b/z.png|cut -c1-1)  索引状态='$(git status --porcelain b/z.png)'"

sec "④ 紧接着提交 b/z.png,会不会漏进原始大文件"
git commit -qm "rest"; check "后续提交" b/z.png

sec "⑤ git commit --amend"
mkdir -p c; head -c 2000000 /dev/urandom > c/w.png; git add c/w.png
git commit -q --amend -m amend; check "git commit --amend" c/w.png

sec "⑥ 全仓库检查:有没有任何非软链的二进制被提交进去"
bad=0
for c in $(git rev-list --all); do
  while read -r mode type oid path; do
    [ "$mode" = 120000 ] && continue
    [ "$path" = .gitignore ] && continue
    sz=$(git cat-file -s "$oid")
    [ "$sz" -gt 10000 ] && { echo "    ❌ $(git log -1 --format=%s $c): $path 是 $mode,$sz 字节"; bad=1; }
  done < <(git ls-tree -r "$c")
done
[ $bad -eq 0 ] && echo "    ✅ 所有提交里的二进制都是软链"

sec "⑦ 体积"
echo "    .git = $(du -sh .git|cut -f1)   blob/ = $(du -sh blob|cut -f1)"
git reflog expire --expire-unreachable=now --all; git gc -q --prune=now
echo "    gc --prune=now 后 .git = $(du -sh .git|cut -f1)"
