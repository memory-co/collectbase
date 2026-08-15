# blob 库:二进制外置 + 软链

> 让 collectbase 管的仓库能装下截图、录音、PDF、模型权重,而 git 仓库本身保持在几百 KB 量级。
>
> 主设计见 [`../DESIGN.md`](../DESIGN.md),分支拓扑见 [`branch-topology.md`](branch-topology.md)。本篇与分层机制正交,但在 §6 有一处重要交叉——**它在事实层的地板上开了个洞**。
>
> 文中"实测"均来自真实执行(git 2.39.5),脚本见同目录 `exp-*.sh`。

---

## 0. 一句话

> **二进制文件不进 git,只进 `blob/`;git 里留一条指向它的相对软链。** 软链的内容是 90 字节的路径字符串,git 存的就是这 90 字节。文件改了就是新 sha、新 blob、新软链;旧 blob 留着,所以旧提交仍然解析得开。

实测:一个 5 MB 的截图 + 一个 8 MB 的 iso,`.git` **168 KB**,`blob/` 4.8 MB。

```
project/log/screen.png -> ../../blob/2026/07/23/f1a4f247…ac.png
                                                  └ sha256

git ls-tree HEAD project/log/screen.png
120000 blob 3badbf45…    project/log/screen.png       ← 模式 120000 = 软链
git cat-file -s HEAD:project/log/screen.png
90                                                     ← git 里就这 90 字节
```

这本质上是**手搓的 git-lfs**,两处不同,都是有意的:

| | git-lfs | 本设计 |
|---|---|---|
| git 里存什么 | 指针文本文件 | **软链** |
| 不装工具时 | 拿到一个指针文本,程序读不了 | **拿到真文件**,任何程序直接读 |
| 存储布局 | `.git/lfs/objects/<sha[0:2]>/…` | `blob/<年>/<月>/<日>/<sha>.<ext>`,**可当媒体库浏览** |

对 harness 场景,"智能体 `open('screenshot.png')` 直接拿到字节"这一条价值很高——不需要它知道 blob 库的存在,也不需要任何 smudge 过滤器。

---

## 1. 判据:`file --mime-encoding`

**规则:头 8 KB 里有 NUL 字节的,入库。** 两条例外。

判据实现在 `collectbase.blob.is_binary`,**纯 Python,不 shell out 到 `file(1)`**——git hook 里少一个外部依赖。它与 `file -b --mime-encoding` 在整个语料上逐项吻合(见下表),而"两边必须是同一份实现"这条约束比"用哪个判据"重要得多。

最初的想法是"非 `text/plain` 的都入库",实测证明这条会把事实层拆了:

| 文件 | `--mime-type` | `--mime-encoding` | 规则 A(非 text/plain) | **规则 B(= NUL 判据)** |
|---|---|---|---|---|
| `s.jsonl` | **`application/x-ndjson`** | `us-ascii` | ❌ 入库 | ✅ 留 |
| `s.json` | `application/json` | `us-ascii` | ❌ 入库 | ✅ 留 |
| `s.py` | `text/x-script.python` | `us-ascii` | ❌ 入库 | ✅ 留 |
| `s.sh` | `text/x-shellscript` | `us-ascii` | ❌ 入库 | ✅ 留 |
| `empty.txt` | `inode/x-empty` | **`binary`** | ❌ 入库 | ⚠️ 需例外 |
| `s.png` | `image/png` | `binary` | ✅ 入库 | ✅ 入库 |
| `big.log` 200KB | `text/plain` | `us-ascii` | ✅ 留 | ✅ 留 |

`.jsonl` 是 `application/x-ndjson` 而不是 `text/plain` —— 会话记录、日志、结构化事实,全都会被搬进 blob 库,而那正是最需要留在 git 里 diff 和 grep 的东西。`mime-type` 描述的是"这是什么格式",`mime-encoding` 描述的才是"这玩意儿是不是字节流",后者正是我们要问的。

### 两条例外

1. **`inode/x-empty` 留下。** 空文件的 encoding 也报 `binary`,但空文件进 blob 库毫无意义。
2. **超大文本兜底(可配,默认 50 MB)。** encoding 判据管不了"一个 200 MB 的纯文本日志"。这条有张力——大文本恰恰是最该留在 git 里的东西(可 diff、可 grep),所以阈值应该定得高,只当病态情况的保险丝,而不是常规路径。

另外允许在 `.collectbase/config.yaml` 里用 glob 强制覆盖两个方向(锚定文件 `layers` 保持最小,只放层名):

```yaml
blob:
  threshold: 50MB
  force_in:                       # 即便判成文本也入库
    - "**/*.parquet"
  force_out:                      # 即便判成二进制也留在 git
    - "**/*.min.js"
```

---

## 2. 执行点:在 `pre-commit` 里改写索引

**git 没有任何在 `git add` 时触发的钩子**,clean 过滤器也不行(过滤器只能改内容,改不了文件模式,变不出软链)。但 `git commit` 完全在我们掌控里,所以做法是:

> **`pre-commit` 里把原本 add 进去的内容踢掉,换成软链,再让 git 提交。**

```sh
# pre-commit(要点见下,少一条就有一种提交方式会漏)
{ git diff --cached --name-only --diff-filter=ACMRT
  git diff        --name-only --diff-filter=ACMRT ; } | sort -u |
while read -r f; do
  [ -L "$f" ] && continue                                   # 已是软链,跳过
  [ "$(file -b --mime-encoding "$f")" = binary ] || continue
  orphan=$(git rev-parse ":$f")                             # 记下将被孤立的对象
  …移进 blob,原地建相对软链…
  git add "$f"                                              # 索引条目 100644 → 120000
  echo "$f $orphan" >> .git/cb-converted                    # 交给 post-commit
done
```

**四种提交方式全部实测通过**(实验 6):

| 提交方式 | 钩子看到的 `GIT_INDEX_FILE` | 结果 |
|---|---|---|
| `git commit`(先 `git add`) | `.git/index` | ✅ 提交里是软链,87 字节 |
| `git commit -a` | `.git/index.lock` | ✅ 软链 |
| `git commit -- <path>`(部分提交) | `.git/next-index-*.lock`(临时索引) | ✅ 软链 |
| `git commit --amend` | `.git/index` | ✅ 软链 |

全仓库审计(遍历 `git rev-list --all` 的每一棵树,找有没有非 `120000` 模式的大对象):**干净,没有任何原始二进制进过任何提交。**

### 三条少一条就漏的要点

1. **`--diff-filter` 必须包含 `T`。** 软链被换回普通文件,git 记的是 **T(类型变更)**,不是 M。而"替换一个媒体文件"恰恰**每次都走这条**——第一次是 A,之后全是 T。我第一版脚本用了 `ACM`,`git commit -a` 就直接漏了一个 2 MB 的原文件进提交,而且表面上钩子"正常执行、处理了 0 个"。这是本篇最阴的一个坑。

2. **必须改工作区,不能只改索引。** `git commit -a` 的暂存动作发生在钩子**之后**,钩子改的索引条目会被随后的暂存覆盖。所以钩子要把工作区里的真文件换成软链——之后 git 无论怎么暂存,拿到的都是软链。

3. **部分提交要用 `post-commit` 补主索引。** `git commit -- <path>` 时钩子拿到的是临时索引(`next-index-*.lock`),改动对本次提交有效,但**主索引里其他已暂存路径仍留着转换前的条目**。`post-commit` 里对转换过的路径重新 `git add` 一遍即可。实测:紧接着提交剩下那个文件,进去的仍然是软链。

`post-commit` 同时负责清掉记下的孤儿对象(见下)。

### 孤儿对象:定点清除

`git add project/big.iso` 那一刻,原始字节已经写进 `.git/objects` 了。钩子换掉索引条目只是让它变成**不可达对象**,并不会删掉它。

**但钩子知道那个对象的 OID**(`git rev-parse :<path>`,在替换索引条目之前取),所以完全不必全库清理。三种做法实测对照——特意先造一个 `git reset --hard` 丢掉的提交当"后悔药",看谁会误伤它:

| 做法 | 孤儿(8 MB) | `reset --hard` 的后悔药 | `.git` |
|---|---|---|---|
| **A. 定点删松散对象文件**<br>`rm .git/objects/f2/57a82…` | ✅ 清除 | ✅ **完好**,内容可取回 | 7.9 M → **236 K** |
| **B. `git prune --expire=now`** | ✅ 清除 | ✅ **完好**,内容可取回 | 7.9 M → **232 K** |
| C. `reflog expire --expire-unreachable=now --all` + `gc --prune=now` | ✅ 清除 | ❌ **没了** | → 172 K |

A 之后 `git fsck` 全库自检无任何 error。

> **更正一处此前的判断:**`git prune --expire=now` **本身尊重 reflog**,不会毁掉 `reset --hard` 的后悔药。之前把 C 的破坏性算到了 prune 头上,实际上是同行的 `reflog expire` 干的。所以"要清就得毁 reflog"不成立。

**默认走 A**:`git add` 产生的是松散对象(单个大文件不会触发 `gc --auto` 打包),`post-commit` 里按记下的 OID 直接删掉那一个文件即可。比 B 快(不用遍历全部引用),而且并发上更安全——B 会清掉**所有**当前不可达的对象,万一另一个 git 进程正在半途创建对象就会被误伤,这正是 git 默认给两周宽限期的原因。

删之前加一道确认:该 OID 确实不被任何 ref、reflog、索引引用(内容碰巧和历史里某个已有对象相同时,它压根不是新建的,不能删)。这个判断做不了的时候退回 B,并先用 `git prune -n --expire=now` 打印将要删除的清单——实测它精确列出了那一个孤儿:

```
$ git prune -n --expire=now
f257a8243235cd243e963beb71d4bff355ee92ab blob
```

C 永远不用。

### 顺带:`git rm --cached` 是多余的

`git add <path>` 本身就会把索引里的 `100644` 条目替换成 `120000` 软链条目,实测:

```
add 后索引:        100644 1e0702c2…  f.png
只用 git add 之后:  120000 59f0df3a…  f.png
```

先 `git rm --cached` 反而会报 `staged content different from both the file and the HEAD`。钩子里一条 `git add` 就够。

### 不做 watcher

一个常驻进程在文件落地那一刻就转换,`git add` 从头到尾只见得到软链,根本不产生孤儿对象。听起来更干净,但 `pre-commit` 这条路已被实测覆盖四种提交方式,而孤儿对象又能定点清除——watcher 省的只是那一瞬间的磁盘占用。**不值一个常驻进程**,不做。

---

## 3. 布局:内容寻址 + 日期分片

```
blob/2026/07/23/f1a4f24756ad…f801ac.png
     └─ 添加当天  └─ sha256(内容)      └─ 保留原扩展名
```

- **sha256 做文件名**:内容寻址,同一天内天然去重,而且内容被篡改可检测(§6)。
- **软链必须是相对路径且不逃逸出 `blob/`**——见下面的白名单。
- **日期分片**:让 `blob/` 本身能当媒体库浏览,这是这个设计相对 LFS 的主要卖点。日期取**添加时刻**。
- **保留扩展名**:直接进 `blob/` 翻找时,图片浏览器、缩略图工具认得出来。软链那头本来就有扩展名,所以这一条纯粹是为了"库能看"。

### `blob/` 必须被 gitignore

实现时踩到的:`blob/` 若没进 `.gitignore`,`git add -A` 会把库里的文件本身暂存进来,`pre-commit` 判定它们是二进制、再转一次,结果是**一条指向自己的软链**。`cb init` 因此负责写 `.gitignore`,守卫也直接拒绝任何落在 `blob/` 里的跟踪路径——那是存储,不是内容。

### 换一个已入库的文件:先删软链

工作区里那个路径是指向 444 blob 的软链,**直接写会穿过软链撞上只读位**,报一个看着莫名其妙的 EACCES。换内容要先 `rm` 掉软链再写新文件。

### 跨天重复的代价

同一份内容在不同日子被添加两次 → 两个路径、两份拷贝。内容寻址本该白拿的去重,被日期分片打了折。

不打算为此放弃日期布局(它是"媒体库"这个立意的全部)。折中:`cb blob gc --dedup` 扫全库,把 sha 相同的跨天副本换成**硬链接**——路径都还在,占用只剩一份。作为周期性维护,不进热路径。

### 白名单:本机制反过来定义了"什么能进 git"

既然二进制一律转成软链,那 git 里就只剩两种形态。把它写成白名单,`reference-transaction` 照着验(主设计 §11):

```
100644 / 100755   内容不是二进制(否则本该先转成 blob 软链)
120000            目标是相对路径,且解析后落在 blob/ 之内
其余一律拒绝      160000(submodule)、任何别的模式
```

**`pre-commit` 是转换器,守卫是校验器,同一条规则的两面。**没有校验器那一侧的话,凡是不跑 `pre-commit` 的路径都能把裸二进制塞进来——`--no-verify`、`cherry-pick`、`rebase` 都不跑它。实测:旧版守卫只查标签和路径归属时,一个 2 MB 的裸二进制**直接进来了**;加上白名单后被拒:

```
✗ project/raw.png 是二进制却直接进了 git —— 应该先转成 blob 软链
✗ project/leak.txt 是绝对路径软链 → /etc/hostname
✗ project/leak2.txt 的软链逃出 blob/ → ../../../etc/passwd
✗ vendor/sub 是 submodule(160000),不支持
```

纯文本和合法的 blob 软链照常放行。

> **两边的二进制判据必须是同一份实现。**转换器说"是文本不转"、校验器说"是二进制拒绝",提交就永远进不去——死锁。`file` 能读 stdin,所以守卫拿 blob OID 也能用完全相同的判断:`git cat-file blob <oid> | file -b --mime-encoding -`(实测可用)。超大文本阈值同理。

---

## 4. GC:活性由历史决定,不是工作区

**这条最容易写错,写错就丢数据。**

一个 blob 是否还有用,取决于**所有分支的所有提交里有没有软链指向它**,而不是当前工作区里有没有。理由:回到旧提交时,旧软链必须还能解析。实测确认这条成立(checkout 到 `HEAD~1`,旧软链指向 5 MB 的旧 blob,`stat -L` 拿得到)。

所以 `cb blob gc` 的活性扫描是:

```sh
git rev-list --all | while read c; do
  git ls-tree -r "$c" | awk '$1=="120000"'      # 取出所有软链
done | 解析出 blob 路径 | sort -u                # 这就是活集
```

不在活集里的才能删。在分层设计下 `--all` 尤其重要:`layer/*` 和 `stack/*` 都可能持有指向同一 blob 的软链。

---

## 5. `git clean -xdf` 会删光 blob 库

实测确认:`blob/` 是被忽略的目录,`git clean -xdf` 会把它和里面的文件全部删掉。

**不为此改设计。**放进 `.gitignore` 就必然有这个性质,躲不掉;真要躲只能把库挪出工作区,那就没有"媒体库"可浏览了。**恢复靠 collectbase 自己的同步机制**——库在别处有副本,`rsync -a host:path/blob/ blob/` 拉回来即可(§7)。

`cb check` 的 I4 会检测到库为空或大面积缺失,并直接给出那条 rsync,而不是让人对着一堆断链猜发生了什么。

---

## 6. 和分层的交叉:它在地板上开了个洞

**这是本篇最重要的一节。**

软链本身受分层保护:它是一个被 git 跟踪的普通路径,归属某一层,`commit-msg` 校验管得着它。但**软链指向的字节在 git 之外,不受任何保护**。

于是事实层的保证被削弱了:

```
layer/facts 里的  project/log/screen.png  →  blob/2026/07/23/<sha>.png
       ↑ 这条软链改不了(分层规则挡着)        ↑ 这些字节谁都能覆盖
```

智能体改不了那条软链,但它可以直接往那个 blob 路径写——`blob/` 是被忽略的,git 全程看不见,`cb check` 的 I1–I4 也全部照过。一张作为事实的截图可以被悄悄换掉,而仓库里没有任何痕迹。

三道防护,和主设计 §8 的思路一致:

1. **blob 落库即 `chmod 444`。** 挡普通写。实测已在流程里。
2. **重新哈希,和路径里的 sha256 比对(即 `cb check` 的 I4)。** 内容寻址在这里回本了——篡改是**可检测**的,这是普通 gitignore 文件没有的性质。这是必需项,不是诊断工具。
3. **uid 边界。** blob 文件属于 uid A、模式 444,智能体跑 uid B,`chmod` 是内核拒绝。目录加 sticky 位它也删不掉。这是唯一硬的一道,和分支拓扑那边推荐的方案是同一条。

> 一句话:**软链的完整性由 git 保证,blob 的完整性由 `cb check` 的 I4 保证。** 两者缺一,事实层就不完整。

---

## 7. 代价:仓库不再自足

`git clone` 只带走软链,不带走字节。实测:clone 出来 **188 KB**,软链**断链**。

这是把二进制移出 git 的必然结果,不是实现缺陷。相应地:

- 传输就是 `rsync -a blob/ host:path/blob/`,不给命令。"按活集传输"是伪需求——整个库 rsync 一遍更简单,增量由 rsync 自己算。
- clone 之后 `cb check` 明确报告"缺 N 个 blob"并给出那条 rsync,而不是让人对着一堆断链猜。

对 collectbase 的主场景(harness 跑在自己机器上,仓库和库都在本地)影响不大;要跨机器共享时,库必须和仓库一起搬,这一点得写进主设计的部署说明。

---

## 8. CLI

| 命令 | 干什么 |
|---|---|
| `cb check`(I4) | 重新哈希比对,列出缺失与篡改,并给出恢复用的 rsync |
| `cb blob gc [--dedup] [-n]` | 按全历史活集清理;`--dedup` 跨天硬链接合并 |

就这两条。转换是钩子内部行为,不给命令;传输就是 `rsync -a blob/ host:path/blob/`;兜底回收孤儿是 `git prune --expire=now`。**凡是 git 或 rsync 已经能做的,不包。** 完整理由见 [`cli.md`](cli.md) §1。

---

## 9. 实测汇总

脚本:[`exp-blob.sh`](exp-blob.sh)(布局与体积)、[`exp-hook.sh`](exp-hook.sh)(钩子改写索引)、[`exp-prune.sh`](exp-prune.sh)(孤儿对象清理)。

**布局与体积**

| 验证项 | 结果 |
|---|---|
| git 里存的对象 | 模式 `120000`,**90 字节**的路径字符串 |
| 仓库 vs 库体积 | `.git` **168 K** / `blob/` **4.8 M** |
| `git status` 是否干净 | ✅ 空 |
| 文件修改 → 新 blob,旧 blob 保留 | ✅ 两个 blob 并存 |
| 回到旧提交,旧软链能否解析 | ✅ 可解析,5,000,000 字节 |
| 深目录里的相对软链 | ✅ `../../blob/…` 正确 |
| `git clone` | ⚠️ **188 K,断链**——字节不随 clone 传输 |
| `git clean -xdf` | ❌ **库被删光**(接受,靠 rsync 恢复) |

**钩子改写索引**

| 提交方式 | 结果 |
|---|---|
| `git commit` | ✅ 软链,87 字节 |
| `git commit -a` | ✅ 软链(前提:`--diff-filter` 含 `T`,且钩子改工作区) |
| `git commit -- <path>` | ✅ 软链(主索引由 `post-commit` 补) |
| `git commit --amend` | ✅ 软链 |
| 全历史审计:有无非软链的大对象进过提交 | ✅ **无** |
| `--diff-filter=ACM`(漏掉 `T`) | ❌ `commit -a` 漏进 2 MB 原文件,且钩子表面正常 |
| 孤儿对象:定点删松散文件 | ✅ 7.9 M → **236 K**,`git fsck` 无 error,reflog 后悔药完好 |
| 孤儿对象:`git prune --expire=now` | ✅ 7.9 M → **232 K**,**后悔药完好**(prune 尊重 reflog) |
| 对照:`reflog expire` + `gc --prune=now` | ⚠️ 172 K,但**后悔药被毁** —— 不要用 |
| `git rm --cached` 是否必需 | ❌ 多余,`git add` 自己就把 100644 换成 120000 |

---

## 10. 待定

- **写到一半的文件。**钩子在 `pre-commit` 触发时文件通常已经写完,风险低;但 watcher 那条路必须判静默期,不能对还在被写入的文件下手。

- **软链在 Windows 上。**需要开发者模式或管理员权限。当前只面向 Linux harness,但这条会挡住跨平台。
- **`force_in` / `force_out` 的 glob 语义**是否复用 `.gitignore` 的匹配规则。
- **blob 的日期与"事实发生时间"的关系。**现在用的是添加时刻;采集三个月前的会话时,截图会落在今天的日期下。要不要允许 worker 声明一个逻辑日期?
- **同一 blob 被多层引用时的归属。**目前 blob 库不分层,facts 和 beliefs 的软链可以指向同一个 blob(去重的自然结果)。blob 是不可变的,所以无害,但 `cb blob gc` 的活集计算必须跨层,别按层裁剪。

---

## 11. 回写主设计的清单

- **§7 锚定**:blob 的可调项放 `.collectbase/config.yaml` 的 `blob:` 段,不进 `layers`。
- **§9 CLI**:并入 §8 的五条命令。
- **§10 边界**:"不管内容格式"要加一句例外——二进制的**存放位置**归 collectbase 管,内容仍然不管。
- **§14 里程碑**:blob 是独立的一条线,排在 M4;I4 和 `cb check` 一起进。
- **§12 弱点**:补上 §6 那个洞——分层保证只覆盖软链,不覆盖字节;`cb check` 的 I4 是必需品不是可选项。
- **部署说明**(主设计目前没有这一节):clone 不带 blob,跨机器要单独搬库。
