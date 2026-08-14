#!/bin/bash
# 实验 7:孤儿对象能不能定点清除?全局 prune 会不会误伤 reset --hard 的后悔药?
set -u
R=/tmp/claude-1000/-home-twwyzh-collectbase/b57f764e-ac83-4eb8-a1cf-8155af4ae107/scratchpad/exp7
rm -rf "$R"; mkdir -p "$R"; cd "$R" || exit 1
export GIT_AUTHOR_NAME=cb GIT_AUTHOR_EMAIL=cb@x GIT_COMMITTER_NAME=cb GIT_COMMITTER_EMAIL=cb@x
git init -q .; sec() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }
loose() { echo ".git/objects/${1:0:2}/${1:2}"; }
sz() { du -sh .git | cut -f1; }

echo hello > a.txt; git add a.txt; git commit -qm base

sec "准备:制造一个 reset --hard 丢掉的提交(后悔药,只有 reflog 记得)"
echo "重要工作" > important.txt; git add important.txt; git commit -qm "会被 reset 掉的提交"
LOST=$(git rev-parse HEAD); LOSTBLOB=$(git rev-parse HEAD:important.txt)
git reset -q --hard HEAD~1
echo "    丢掉的提交 $LOST"
echo "    它还在吗: $(git cat-file -e $LOST 2>/dev/null && echo 在 || echo 没了)"
echo "    reflog 里能找到吗: $(git reflog | grep -c "$(git rev-parse --short $LOST)") 条"

sec "制造孤儿:add 一个 8MB 二进制,再把索引条目换成软链"
mkdir -p blob; echo "blob/" > .gitignore; git add .gitignore
head -c 8000000 /dev/urandom > big.iso
git add big.iso
ORPHAN=$(git rev-parse :big.iso)
echo "    对象 $ORPHAN  大小 $(git cat-file -s $ORPHAN)"
echo "    松散文件: $(loose $ORPHAN)  存在? $([ -f "$(loose $ORPHAN)" ] && echo 是 || echo 否)"
mv big.iso "blob/$ORPHAN.iso"; ln -s "blob/$ORPHAN.iso" big.iso
git rm -q --cached big.iso; git add big.iso; git commit -qm "换成软链"
echo "    提交后 .git = $(sz)   索引/提交里已不再引用它"
echo "    git fsck 认为它是: $(git fsck --unreachable 2>/dev/null | grep -c "$ORPHAN") 条 unreachable 记录"

sec "方案 A:定点删除那个松散对象文件"
cp -r .git /tmp/exp7-git-backup
rm -f "$(loose $ORPHAN)"
echo "    删掉后 .git = $(sz)"
echo "    对象还在吗: $(git cat-file -e $ORPHAN 2>/dev/null && echo 在 || echo 没了)"
echo "    后悔药还在吗: $(git cat-file -e $LOST 2>/dev/null && echo 在 || echo 没了)"
echo "    git fsck 全库自检:"
git fsck 2>&1 | sed 's/^/      /' | head -5
echo "    (无输出/无 error = 仓库完好)"
echo "    reflog 恢复演练: git cat-file -p $LOST:important.txt →  $(git cat-file -p $LOST:important.txt 2>&1)"

sec "方案 B:git prune --expire=now(全局,但尊重 reflog)"
rm -rf .git; mv /tmp/exp7-git-backup .git; git reset -q --hard 2>/dev/null
echo "    还原后 .git = $(sz),孤儿在? $(git cat-file -e $ORPHAN 2>/dev/null && echo 在 || echo 没了)"
echo "    dry-run(git prune -n --expire=now)列出要删的:"
git prune -n --expire=now 2>/dev/null | sed 's/^/      /' | head
git prune --expire=now
echo "    prune 后 .git = $(sz)"
echo "    孤儿还在吗:     $(git cat-file -e $ORPHAN 2>/dev/null && echo 在 || echo 没了)"
echo "    后悔药还在吗:   $(git cat-file -e $LOST 2>/dev/null && echo 在 || echo 没了)"
echo "    后悔药的内容:   $(git cat-file -p $LOST:important.txt 2>&1)"

sec "方案 C:对照 —— 先 reflog expire 再 gc(我上一轮用的那套)"
git reflog expire --expire-unreachable=now --all; git gc -q --prune=now
echo "    之后 .git = $(sz)"
echo "    后悔药还在吗: $(git cat-file -e $LOST 2>/dev/null && echo 在 || echo 没了)  ← 对比 B"
