# blob 库:二进制外置 + 软链

> 让 collectbase 管的仓库能装下截图、录音、PDF、模型权重,而 git 仓库本身保持在几百 KB 量级。
>
> 主设计见 [`../DESIGN.md`](../DESIGN.md),分支拓扑见 [`branch-topology.md`](branch-topology.md)。本篇与分层机制正交,但在 §6 有一处重要交叉——**它在事实层的地板上开了个洞**。
>
> 文中"实测"均来自真实执行(git 2.39.5),脚本 [`exp-blob.sh`](exp-blob.sh)。

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

**规则:`file -b --mime-encoding` 返回 `binary` 的,入库。** 两条例外。

最初的想法是"非 `text/plain` 的都入库",实测证明这条会把事实层拆了:

| 文件 | `--mime-type` | `--mime-encoding` | 规则 A(非 text/plain) | **规则 B(encoding)** |
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

另外允许在 `collectbase.toml` 里用 glob 强制覆盖两个方向:

```toml
[blob]
threshold  = "50MB"
force_in   = ["**/*.parquet"]     # 即便判成文本也入库
force_out  = ["**/*.min.js"]      # 即便判成二进制也留在 git
```

---

## 2. 执行点:git 没有 `pre-add` hook

这是个硬约束,得先说清楚:**git 没有任何在 `git add` 时触发的钩子。** 所以"add 的时候就转换"没法用钩子实现。三个可选执行点:

| 执行点 | 可行 | 代价 |
|---|---|---|
| `git add` 时 | ❌ **不存在这个钩子** | — |
| clean 过滤器(`.gitattributes`) | ❌ | 过滤器只能改内容,改不了文件模式,变不出软链 |
| **`pre-commit`** | ✅ | `git add` 已经把原始字节写进对象库了,见下 |
| **watcher(`cb watch`)** | ✅ | 需要一个常驻进程 |

### 推荐:watcher 主路径,`pre-commit` 兜底

**watcher** 在文件落地的那一刻就转换,于是 `git add` 从头到尾只见得到软链,对象库里干干净净。harness 场景下这很自然——它本来就要拉起一个长期进程。

**`pre-commit`** 是兜底:漏网的当场转换(移进 blob、建软链、重新 `git add`),提交里进去的是软链。功能上完全正确,但有个残留:

> `git add project/big.iso` 已经在 `.git/objects` 里写了一个 8 MB 的完整对象。之后就算把索引项换成软链,那个对象仍然存在,只是变成了不可达对象。

实测:add 8 MB 文件后 `.git` = **7.9 M**;换成软链提交后仍是 **7.9 M**;`git reflog expire --expire-unreachable=now --all && git gc --prune=now` 之后回到 **168 K**。

所以孤儿对象是清得掉的,但那条命令**会连带清掉所有不可达的 reflog 记录**,`git reset --hard` 之后的救命稻草就没了。因此:

- 默认**不主动清**,交给 git 正常的 gc(不可达对象两周后过期)。临时占点磁盘,安全。
- 提供 `cb blob reclaim` 做立即回收,并在命令输出里明写 reflog 的代价。
- 真在意的话,用 watcher,根本不产生孤儿。

---

## 3. 布局:内容寻址 + 日期分片

```
blob/2026/07/23/f1a4f24756ad…f801ac.png
     └─ 添加当天  └─ sha256(内容)      └─ 保留原扩展名
```

- **sha256 做文件名**:内容寻址,同一天内天然去重,而且内容被篡改可检测(§6)。
- **日期分片**:让 `blob/` 本身能当媒体库浏览,这是这个设计相对 LFS 的主要卖点。日期取**添加时刻**。
- **保留扩展名**:直接进 `blob/` 翻找时,图片浏览器、缩略图工具认得出来。软链那头本来就有扩展名,所以这一条纯粹是为了"库能看"。

### 跨天重复的代价

同一份内容在不同日子被添加两次 → 两个路径、两份拷贝。内容寻址本该白拿的去重,被日期分片打了折。

不打算为此放弃日期布局(它是"媒体库"这个立意的全部)。折中:`cb blob gc --dedup` 扫全库,把 sha 相同的跨天副本换成**硬链接**——路径都还在,占用只剩一份。作为周期性维护,不进热路径。

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

## 5. `blob/` 本身应该是一条软链

`git clean -xdf` 会删掉被忽略的文件——**包括整个 blob 库**。实测对照:

| `blob` 是什么 | `git clean -xdf` 之后 |
|---|---|
| 真目录 | ❌ **目录和里面的文件全没了** |
| 指向仓库外存储的软链 | ✅ 软链被删,**存储完好无损**(文件还在) |

所以默认形态是:

```
blob -> ~/.collectbase/store/<repo-id>/
.gitignore:  blob
```

`git clean -xdf` 顶多删掉那条软链,`cb doctor` 一条命令重建。库在仓库之外,顺带也让"多个 clone 共享同一个库"和"库单独 rsync"变得自然。

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
2. **`cb blob verify`:重新哈希,和路径里的 sha256 比对。** 内容寻址在这里回本了——篡改是**可检测**的,这是普通 gitignore 文件没有的性质。应该进 `cb check`,并且在事实层的 blob 上默认开启。
3. **uid 边界。** blob 文件属于 uid A、模式 444,智能体跑 uid B,`chmod` 是内核拒绝。目录加 sticky 位它也删不掉。这是唯一硬的一道,和分支拓扑那边推荐的方案是同一条。

> 一句话:**软链的完整性由 git 保证,blob 的完整性由 `cb blob verify` 保证。** 两者缺一,事实层就不完整。

---

## 7. 代价:仓库不再自足

`git clone` 只带走软链,不带走字节。实测:clone 出来 **188 KB**,软链**断链**。

这是把二进制移出 git 的必然结果,不是实现缺陷。相应地:

- `cb blob push <remote>` / `cb blob pull <remote>` —— rsync 一层薄封装,按活集传输。
- `cb blob verify --missing` —— 列出当前分支引用了但本地不存在的 blob。
- clone 之后 `cb doctor` 应当明确报告"缺 N 个 blob",而不是让人对着一堆断链猜。

对 collectbase 的主场景(harness 跑在自己机器上,仓库和库都在本地)影响不大;要跨机器共享时,库必须和仓库一起搬,这一点得写进主设计的部署说明。

---

## 8. CLI

| 命令 | 干什么 |
|---|---|
| `cb watch` | 常驻,文件落地即转换(推荐路径) |
| `cb blob verify [--missing]` | 重新哈希比对 / 列出缺失的 blob |
| `cb blob gc [--dedup]` | 按全历史活集清理;`--dedup` 跨天硬链接合并 |
| `cb blob reclaim` | 立即回收误 add 产生的孤儿对象(会清 reflog,命令自己会警告) |
| `cb doctor` | 重建 `blob` 软链、报告缺失 |

`pre-commit` 里的转换不给单独命令,它是钩子内部行为。

---

## 9. 实测汇总

| 验证项 | 结果 |
|---|---|
| git 里存的对象 | 模式 `120000`,**90 字节**的路径字符串 |
| 仓库 vs 库体积 | `.git` **168 K** / `blob/` **4.8 M** |
| `git status` 是否干净 | ✅ 空 |
| 文件修改 → 新 blob,旧 blob 保留 | ✅ 两个 blob 并存 |
| 回到旧提交,旧软链能否解析 | ✅ 可解析,5,000,000 字节 |
| 深目录里的相对软链 | ✅ `../../blob/…` 正确 |
| 误 `git add` 8 MB 原文件 | `.git` 涨到 **7.9 M**,换软链提交后仍是 7.9 M |
| `reflog expire + gc --prune=now` | ✅ 回到 **168 K**,孤儿对象清除 |
| `git clone` | ⚠️ **188 K,断链**——字节不随 clone 传输 |
| `git clean -xdf`(blob 为真目录) | ❌ **库被删光** |
| `git clean -xdf`(blob 为外部软链) | ✅ 只删软链,库完好 |

---

## 10. 待定

- **watcher 的形态。**inotify?轮询?和 `cb sync` 是同一个进程还是两个?写到一半的文件(还在被写入)不能立刻转换,需要静默期判断。
- **软链在 Windows 上。**需要开发者模式或管理员权限。当前只面向 Linux harness,但这条会挡住跨平台。
- **`force_in` / `force_out` 的 glob 语义**是否复用 `.gitignore` 的匹配规则。
- **blob 的日期与"事实发生时间"的关系。**现在用的是添加时刻;采集三个月前的会话时,截图会落在今天的日期下。要不要允许 worker 声明一个逻辑日期?
- **同一 blob 被多层引用时的归属。**目前 blob 库不分层,facts 和 beliefs 的软链可以指向同一个 blob(去重的自然结果)。blob 是不可变的,所以无害,但 `cb blob gc` 的活集计算必须跨层,别按层裁剪。

---

## 11. 回写主设计的清单

- **§7 配置**:`collectbase.toml` 增加 `[blob]` 段(阈值、force_in / force_out)。
- **§9 CLI**:并入 §8 的五条命令。
- **§10 边界**:"不管内容格式"要加一句例外——二进制的**存放位置**归 collectbase 管,内容仍然不管。
- **§11 里程碑**:blob 库是独立的一条线,建议排在 M3 之后、M4 之前;`cb blob verify` 要和 `cb check` 一起进 M4。
- **§12 弱点**:补上 §6 那个洞——分层保证只覆盖软链,不覆盖字节;`cb blob verify` 是必需品不是可选项。
- **部署说明**(主设计目前没有这一节):clone 不带 blob,跨机器要单独搬库。
