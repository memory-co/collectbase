#!/bin/bash
# 实验 4:blob 外置 + 软链。验证 git 里存什么、仓库有多大、历史能不能解析、误加大文件的残留。
set -u
R=/tmp/claude-1000/-home-twwyzh-collectbase/b57f764e-ac83-4eb8-a1cf-8155af4ae107/scratchpad/exp4
rm -rf "$R"; mkdir -p "$R"; cd "$R" || exit 1
export GIT_AUTHOR_NAME=cb GIT_AUTHOR_EMAIL=cb@x GIT_COMMITTER_NAME=cb GIT_COMMITTER_EMAIL=cb@x
git init -q .; sec() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }
DAY=2026/07/23

echo "blob/" > .gitignore; git add .gitignore; git commit -qm "[cb] init"

blobify() {  # $1 = 工作区里的真实文件 → 移进 blob,原地留相对软链
  local f=$1 sha ext dst rel
  sha=$(sha256sum "$f" | cut -c1-64); ext=${f##*.}; [ "$ext" = "$f" ] && ext=bin
  dst="blob/$DAY/$sha.$ext"; mkdir -p "$(dirname "$dst")"
  [ -e "$dst" ] || { mv "$f" "$dst"; chmod 444 "$dst"; }
  rm -f "$f"
  rel=$(realpath --relative-to="$(dirname "$f")" "$dst")
  ln -s "$rel" "$f"
}

sec "① 深目录里的大文件 → blob + 软链"
mkdir -p project/log
head -c 5000000 /dev/urandom > project/log/screen.png
blobify project/log/screen.png
ls -l project/log/screen.png | sed 's/^/    /'
git add -A && git commit -qm "[facts] 截图"
echo "    git 里存的对象类型/大小/内容:"
git cat-file -t HEAD:project/log/screen.png | sed 's/^/      type: /'
git cat-file -s HEAD:project/log/screen.png | sed 's/^/      size: /'
git cat-file -p HEAD:project/log/screen.png | sed 's/^/      内容: /'
echo "    ls-tree 模式:"; git ls-tree HEAD project/log/screen.png | sed 's/^/      /'

sec "② 仓库大小 vs blob 大小"
git gc -q 2>/dev/null
printf '    .git      %s\n    blob/     %s\n' "$(du -sh .git | cut -f1)" "$(du -sh blob | cut -f1)"

sec "③ git status 是否干净(blob/ 被忽略)"
git status --porcelain | sed 's/^/    /'; echo "    (空 = 干净)"

sec "④ 文件被修改 → 新 sha → 新软链,旧 blob 保留"
head -c 3000000 /dev/urandom > project/log/screen.png.new
rm project/log/screen.png; mv project/log/screen.png.new project/log/screen.png
blobify project/log/screen.png
git add -A && git commit -qm "[facts] 截图 v2"
echo "    当前软链:"; readlink project/log/screen.png | sed 's/^/      /'
echo "    blob 目录:"; find blob -type f | sed 's/^/      /'

sec "⑤ 回到旧提交,软链能否解析"
git checkout -q HEAD~1
echo "    HEAD~1 的软链: $(readlink project/log/screen.png)"
if [ -e project/log/screen.png ]; then echo "    → 可解析,大小 $(stat -Lc%s project/log/screen.png)"; else echo "    → 断链"; fi
git checkout -q -

sec "⑥ 误把大文件直接 git add 了会怎样(不经过 blobify)"
head -c 8000000 /dev/urandom > project/big.iso
git add project/big.iso
echo "    add 之后 .git 大小: $(du -sh .git | cut -f1)"
oid=$(git rev-parse :project/big.iso); echo "    产生的对象: $oid ($(git cat-file -s $oid) 字节)"
git rm -q --cached project/big.iso; blobify project/big.iso
git add -A && git commit -qm "[facts] iso"
echo "    换成软链提交后 .git 大小: $(du -sh .git | cut -f1)  ← 孤儿对象还在"
git reflog expire --expire-unreachable=now --all; git gc -q --prune=now
echo "    gc --prune=now 之后:  $(du -sh .git | cut -f1)"
git cat-file -e "$oid" 2>/dev/null && echo "    孤儿对象仍在" || echo "    孤儿对象已清除"

sec "⑦ clone 出去会怎样(blob 不跟着走)"
cd ..; rm -rf exp4-clone; git clone -q exp4 exp4-clone; cd exp4-clone
echo "    clone 大小: $(du -sh .git | cut -f1)"
echo "    软链: $(readlink project/log/screen.png)"
[ -e project/log/screen.png ] && echo "    → 可解析" || echo "    → 断链(blob 未随 clone 传输)"

sec "⑧ 判据对比:非 text/plain vs mime-encoding=binary"
cd ../exp4
printf '{"a":1}\n' > t.jsonl; head -c 100 /dev/urandom > t.png; : > t.empty; python3 -c "open('t.log','w').write('x'*200000)"
for f in t.jsonl t.png t.empty t.log; do
  mt=$(file -b --mime-type "$f"); me=$(file -b --mime-encoding "$f"); sz=$(stat -c%s "$f")
  r1=$([ "$mt" = text/plain ] && echo 留 || echo 入库)
  r2=$([ "$me" = binary ] && echo 入库 || echo 留); [ "$mt" = inode/x-empty ] && r2=留
  [ "$sz" -gt 1048576 ] && r2="入库(超阈值)"
  printf '    %-10s %-26s %-10s  规则A:%-4s 规则B:%s\n' "$f" "$mt" "$me" "$r1" "$r2"
done
rm -f t.*
