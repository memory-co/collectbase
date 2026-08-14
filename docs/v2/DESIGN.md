# collectbase v2 — 分层记录文件系统:层即分支(设计)

> **状态:设计中(founding doc)。** v2 是一次**定位转向**。v1 的 engine / worker / sink 三角色——监听上游、增量读、归一成 session、推给 memory system——**整体作废**,不再保留。
>
> v2 的 collectbase 只干一件事:**把智能体的信息记录层做成一个分层文件系统**。最下面是事实层,智能体够不着;上层是它自己的推论,可以反复重写。底层实现是 **Git 分支**,对外接口**也是 Git**——用户和智能体敲的是 `git checkout` / `git commit` / `git log`,collectbase 提供的是让 Git 表现出分层语义的那套东西:分支拓扑 + hook。
>
> 名字仍叫 collectbase(collect = 把智能体积累的东西**收拢**成有结构的一叠),但它现在是一个**工具**,不是一个服务。
>
> v1 设计存档在 [`../v1/`](../v1/),已停止演进。

**本篇是立意 + 契约。**机制的推导过程、被否掉的方案、以及全部实测数据在子设计里:

- [`works/branch-topology.md`](works/branch-topology.md) — **分支怎么组织**。单写入面、`[层名]` 声明、投影与重算、全仓库零 merge 节点。附两个被否方案及其实测。
- [`works/blob-store.md`](works/blob-store.md) — **二进制怎么存**。`blob/` 外置 + 软链、`file --mime-encoding` 判据、钩子改写索引在四种提交方式下的行为、孤儿对象定点清除。

---

## 0. 一句话

> **给智能体一块它够不着的地板。** 事实放最底层,只读;推论放上层,随便改。层与层之间**路径不相交**——上层永远碰不到下层已占的路径,所以没有覆盖、没有遮蔽、没有冲突,也就不需要任何合并策略。

```
git checkout stack/beliefs   →   工作树 = facts ∪ notes ∪ beliefs    ← 唯一的写入面
git checkout stack/notes     →   工作树 = facts ∪ notes              ← 只读视图
git checkout layer/notes     →   工作树 = 只有 notes 自己的文件       ← 只读视图
git checkout layer/facts     →   工作树 = 只有事实                   ← 只读视图
```

日常动作全是 Git 的。`cb` 只出现在三个地方:`init`、诊断、和 hook 内部。

---

## 1. 为什么要分层

智能体的记忆会腐烂,腐烂的方式很具体:**它把自己的推论当成观测,下一轮基于那个推论再推**。三五轮之后,内容还是自洽的、还是引用得头头是道,只是跟现实脱钩了。这不是模型不够聪明,是它的工作记忆里**观测和推论长得一模一样**——都是文件,都能改,改完看不出区别。

分层就是在文件系统这一层把这条回路切断:

- **事实层**放真正发生过的东西——会话记录、命令输出、外部文档、人写下的约束。谁写它取决于场景(采集脚本、人、外部同步),但**智能体不写**。
- **上层**放智能体自己的东西:摘要、结构化的记忆、假设、结论。它可以把上一版全推翻重来,这是它该干的事。
- 上层**改不动**下层。

所以演化是有地板的:无论上层怎么迭代,重新推导的原料永远是那份没被动过的事实。

> 这不是访问控制,是**认知卫生**。目的不是防坏人,是让"我看到的"和"我以为的"在文件系统上是两种东西。防对抗要另外的手段,见 §12。

---

## 2. 核心约束:路径全局唯一

**一个路径只属于一层。上层不能占用下层已有的路径,下层也不能新增一个上层已占的路径。**

这条约束是整个设计的地基,它一次性消掉了一大堆东西:

| 不需要的东西 | 为什么不需要 |
|---|---|
| 遮蔽 / override | 同路径根本不会出现在两层 |
| copy-up(写时复制) | 上层想改下层文件?不允许,只能另写一份 |
| whiteout(删除标记) | 上层没有任何手段让下层文件消失 |
| 合并策略 / 冲突解决 | 层的组合是**不相交并集**,不是 merge |

Docker 的类比只到"叠"为止——**overlay 那一整套全不要**。

三个直接后果,都是想要的:

1. **智能体没有"修改"这个动作,只有"追加"。** 它认为下层某条结论错了,不能改那个文件,只能在自己层里另写一份、引用它。修正变成注解而非覆盖:原文永远在,谁在什么时候提出异议是可 diff 的。
2. **删除也跨不了层。** 上层连"假装没看见"都做不到。这比"不可编辑"更强。
3. **不相交让"组合"退化成一个纯粹的树运算。** 各层的树可以直接拼进一个索引,不需要三方合并,也就不可能有冲突——这才是选 Git 分支的真正理由。

**布局完全自由。**layer2 可以把 `api-analysis.md` 放在 layer1 的 `api.md` 旁边、同一个目录里。不强制"每层一个顶层目录"——那样等于宣布层之间不许在结构上贴近,把分层的价值卖掉了。

---

## 3. 拓扑:一条写入面,两族派生分支

```
                        写                       派生(只读)
                        ↓
stack/beliefs   ●──●──●──●──●         ← 唯一的写入面,也是全局时间线
                                        工作树 = facts ∪ notes ∪ beliefs

stack/notes     ●──●──●               ← 读视图:facts ∪ notes
layer/facts     ●──●                  ← 只有事实层自己的文件 + 自己的历史
layer/notes     ●
layer/beliefs   ●──●
```

- **写只发生在最长那条 `stack/*` 上。**所有人、所有层,都提交到这一条。
- **提交信息必须以 `[层名]` 开头**声明属于哪一层(§5)。
- 接收后由 `cb` **投影**到对应的 `layer/*`,并**重算**所有包含该层的更短 `stack/*`(§6)。
- 其余分支全是派生的只读视图,没人往上面提交。
- 最底层不需要单独的 stack 分支:`stack/facts` 与 `layer/facts` 内容和历史都相同,只建后者。

**为什么写入面只能有一条。**若允许在较短的 stack 上写,更长那条要跟进就只有两条路:单亲重算(同一事件出现两个 commit 对象,血缘断开),或者 merge 进来(引入 merge 节点)。收敛成一条之后这个二选一消失:**每个事件有且只有一个权威 commit 对象,其余分支上的一切都是它的投影,血缘、顺序、权威三者一致。**`layer/*` 的历史因此是写入面历史的**子序列**——`git log layer/beliefs` 恰好就是"智能体自己干过的事"。

代价是写入必须串行化,见 §12。

### 五条不变量

- **I1 不相交** — 任意两层的路径集无交集。
- **I2 一致** — `tree(stack/Lk) == union(tree(layer/L1..Lk))`。
- **I3 线性** — 全仓库无 merge 节点,每条分支的每次前进都是 fast-forward。
- **I4 对应** — `layer/*` 的每个提交都有 `Cb-Stack` trailer 指回写入面上的源提交。
- **I5 blob 完整** — 每条 `120000` 软链指向的 blob 存在,且内容 sha256 与路径中的哈希一致。

`cb check` 校验这五条。它们被破坏时,不是警告,是仓库坏了。

> I1–I4 已在 [`works/exp-single-face.sh`](works/exp-single-face.sh) 里实测通过:五次提交序列后 merge 节点数为 0,所有分支全程 fast-forward,I2 逐字成立。

---

## 4. 归属:一个路径归哪层

```
own(Lᵢ) = tree(layer/Lᵢ) 里的全部路径
```

就这么直接——`layer/*` 分支只含本层文件,它的树**就是**归属表,不用做集合差,也不用另立登记表。规则和事实是同一份数据,不可能出现"登记说归 A、实际在 B"。

**新路径归声明的层。**提交信息里的 `[层名]` 就是那个信号,不靠推断。

**归属稳定**:一个路径一旦被某层引入,永远归那层。想搬家就是下层删、上层加,两次提交,历史里看得见。

---

## 5. 拦截:`commit-msg` 三条校验

```
① chmod a-w        写下层文件的那一刻就 EACCES    ← 最快,给智能体最直白的信号
② commit-msg       提交时拒绝                     ← 主拦截,可被 --no-verify 绕过
③ 投影器隔离       非法提交进不了 layer/*          ← 兜住 ② 被绕过的情况
④ pre-receive      push 时拒绝                    ← 唯一不可绕的(暂不做,见 §13)
```

**本版做 ①②③。**

### 为什么是 `commit-msg` 而不是 `pre-commit`

钩子顺序是 `pre-commit → prepare-commit-msg → commit-msg`。`pre-commit` 跑的时候**提交信息还不存在**,而这道校验需要同时看到信息(声明的层)和暂存区(实际改动的路径)。只有 `commit-msg` 两样都有(`git diff --cached` 在其中照常可用)。

`pre-commit` 另有职责:blob 转换(§8)。

### 三条校验

```
1. 当前分支不是写入面                  → 拒绝(其余分支都是派生的只读视图)
2. 无 [层名] 前缀 / 层名不在配置里       → 拒绝
3. 改动的路径已归属于别的层             → 拒绝,并指出归谁
```

实测的拒绝信息:

```
拒绝:'project/api.md' 属于层 [facts],本次提交声明的是 [beliefs]
拒绝:提交信息必须以 [层名] 开头
```

**一个提交只能属于一层。**想"顺手把事实和笔记一起提交了"是不行的,得拆两次。这是特性不是限制——一个跨层的提交,恰好就是把观测和推论搅在一起的那个动作。

### `[层名]` 的三个定死的细节

- **用层名,不用序号。**`[facts]` 而不是 `[layer1]`。中途插一层会让序号集体错位,历史里的旧标签全部失真。
- **放 subject 开头,不放 trailer。** trailer 在 `--oneline` 里看不见,而"一眼看出每个提交属于哪层"是它一半的价值。
- **字符集受限**(`[a-z][a-z0-9_-]*`),因为它同时是分支名的一部分。

### ① post-checkout:锁住下层

切到写入面之后,把不属于顶层的文件 `chmod a-w`。两件事让这个土办法能用,都已实测:

- **不产生假 diff。** Git 只记录可执行位(100644 / 100755),不记录写位。644→444 在 Git 眼里毫无变化。
- **不妨碍 Git 自己干活。** 实测 `git checkout HEAD~1 -- <file>` 能成功覆写一个 444 文件——所以**不需要"解锁 → 操作 → 上锁"的包装**。代价是覆写后权限重置为 644,因此每次 checkout / sync 之后必须重打一遍 chmod。

---

## 6. 传播:投影 + 重算

一次合法提交落在写入面上之后,`cb`(由 `post-commit` 触发)做两件事,都是单亲提交,都不产生 merge 节点:

```
提交 [notes] 落在 stack/beliefs
   ↓ 投影   → layer/notes          取子集
   ↓ 重算   → stack/notes          重算并集树
   ─ 不动   → layer/facts、layer/beliefs、不含 notes 的更短 stack
```

全部机制归结成两个树运算,都不碰 Git 的合并机制:

**并集树**(造 `stack/*`)。路径不相交,所以直接拼索引:

```sh
for L in facts notes; do
  git ls-tree -r "refs/heads/layer/$L" | GIT_INDEX_FILE=$idx git update-index --index-info
done
tree=$(GIT_INDEX_FILE=$idx git write-tree)
```

`git ls-tree -r` 的输出格式正是 `update-index --index-info` 吃的格式,直接管道。重复路径会被**静默覆盖**,所以必须自己查 `uniq -d` 并报错——好处是错误信息由我们写,能说清"这个路径归 facts,换一个"。

**过滤树**(造 `layer/*`)。同一套 plumbing,把写入面的 `ls-tree` 输出按本层路径白名单过一遍。

两者都用 `git commit-tree $tree -p <上一个 tip>` 单亲成链。投影出来的提交**逐字复制**原提交信息,并追加 `Cb-Stack: <sha>` trailer——两个 commit 对象的 SHA 必然不同(树是子集、父亲不同),所以对应关系必须显式记录。

> 细节与实测见 [`works/branch-topology.md`](works/branch-topology.md) §4–§5。

### 权威:stack,但违规提交不放行

**写入面是权威。**已接收的提交以它为准,不会被重建抹掉,工作不会丢。

但权威不等于投影器无条件服从。`--no-verify` 能跳过 `commit-msg`,一个改了事实层文件的提交是可能进入写入面的。这时**投影器停下报错**,把写入面标记为 dirty:既不静默照做(那会把改动写进 `layer/facts`,智能体就真的改到了事实层),也不静默回滚(那会丢工作)。`cb check` 报 divergence,`cb sync` 拒绝继续,等人处理。

> 这是本设计里**唯一**需要人介入的状态。它只可能由 `--no-verify` 或直接写 `.git/` 产生。

---

## 7. 配置:层定义放在最底层

根目录一个 `collectbase.toml`,顺序即层序,自下而上:

```toml
[[layer]]
name = "facts"        # 同时是提交信息里的标签,受 [a-z][a-z0-9_-]* 约束

[[layer]]
name = "notes"

[[layer]]
name = "beliefs"

[blob]
threshold = "50MB"                 # 超大文本的兜底阈值
force_in  = ["**/*.parquet"]       # 即便判成文本也入 blob 库
force_out = ["**/*.min.js"]        # 即便判成二进制也留在 git
```

分支名由层名推出:`layer/<name>`、`stack/<name>`。

**这个文件归最底层所有。**于是层的定义本身也是智能体够不着的东西——它不能给自己加一层、不能改层序、不能把事实层从栈里摘掉。写入面的树里始终有这份文件(它含所有层),hook 从 HEAD 直接读即可,没有引导难题。

### hook 跟着仓库走

Git 的 hook 默认在 `.git/hooks/`,**不随 clone 传播**。所以:

```sh
git config core.hooksPath .collectbase/hooks
```

hook 脚本本体放在 `.collectbase/hooks/`,由最底层跟踪,是仓库内容的一部分,clone 出来就在。只剩 `git config` 那一行需要 `cb init` 在每个 clone 里跑一次(Git 的安全设计,绕不过,也不该绕)。

每个 hook 脚本都是一行,逻辑全在 `cb` 里:

```sh
#!/bin/sh
exec cb hook commit-msg "$@"
```

---

## 8. 二进制:blob 库

二进制文件不进 git,只进 `blob/`,git 里留一条指向它的相对软链:

```
project/log/screen.png -> ../../blob/2026/07/23/f1a4f247…ac.png
                                     └ 添加当天  └ sha256    └ 原扩展名

git ls-tree HEAD project/log/screen.png
120000 blob 3badbf45…    project/log/screen.png      ← 模式 120000,git 里就 90 字节
```

实测:5 MB 截图 + 8 MB iso 的仓库,`.git` **168 KB**,`blob/` 4.8 MB。

- **判据**:`file -b --mime-encoding` 返回 `binary` 的入库。**不能用 `--mime-type`**——`.jsonl` 是 `application/x-ndjson`、`.json` 是 `application/json`,按"非 `text/plain` 入库"会把事实层的会话记录整个搬进 blob 库。例外:`inode/x-empty` 留下(空文件的 encoding 也报 binary)。
- **执行点**:`pre-commit` 里把原本 add 进去的内容踢掉、换成软链、再让 git 提交。实测 `git commit` / `-a` / 部分提交 / `--amend` 四种方式全部得到软链,全历史审计无任何原始二进制进过提交。
- **孤儿对象**:`git add` 那一刻原始字节已写进 `.git/objects`。钩子在替换前用 `git rev-parse :<path>` 记下 OID,`post-commit` 定点删掉那一个松散对象文件即可(实测 7.9 M → 236 K,`git fsck` 无 error,reflog 完好)。
- **GC 活性由历史决定**,不是工作区:遍历 `git rev-list --all` 所有树里的 `120000` 条目才是活集。否则回到旧提交时旧软链就断了。

> 三条少一条就漏的实现要点(`--diff-filter` 必须含 `T`、必须改工作区、部分提交要 `post-commit` 补主索引)见 [`works/blob-store.md`](works/blob-store.md) §2。**这三条都是实测撞出来的,不是推演。**

---

## 9. 用起来是什么样

```sh
# 一次性
cb init --layers facts,notes,beliefs
# 建 layer/* 与 stack/*、写 collectbase.toml、装 hook、配 core.hooksPath

git checkout stack/beliefs          # 唯一的写入面,之后不用再切

# 事实进来(人 / 采集脚本 / 外部同步)
cp ~/.claude/projects/…/session.jsonl project/log/2026-08-14.jsonl
git add -A && git commit -m "[facts] 采集 8-14 会话"

# 智能体上班(工作树里 facts、notes 全在且是 444,只有 beliefs 的路径可写)
$EDITOR project/log/why-it-broke.md
git commit -am "[beliefs] 这次故障的根因判断"

# 它试图动事实
$EDITOR project/log/2026-08-14.jsonl        # → EACCES,当场失败
# 就算它 chmod 回来强行改了:
git commit -am "[beliefs] 顺手修一下"
# → 拒绝:'project/log/2026-08-14.jsonl' 属于层 [facts],本次提交声明的是 [beliefs]

# 只看某一层 / 某一层的演化
git checkout layer/notes
git log --oneline layer/beliefs              # 只有智能体自己干过的事

# 全局时间线,自带层标注
git log --oneline stack/beliefs
#   * [beliefs] 推翻上一版根因
#   * [facts]   采集 8-15 日志
#   * [beliefs] 这次故障的根因判断
#   * [notes]   api 的调用约束笔记
```

---

## 10. CLI 表面

刻意小。Git 是接口,`cb` 不包装 Git 的日常动词。

| 命令 | 干什么 |
|---|---|
| `cb init --layers a,b,c` | 建分支拓扑、写配置、装 hook |
| `cb sync` | 投影 + 重算(hook 调它,也可手动) |
| `cb check` | 校验 I1–I5,列出违反项 |
| `cb owner <path>` | 这个路径归哪层 |
| `cb status` | `git status` 的分层版:改动按层分组,越界的标红 |
| `cb layers` | 列出层、分支、各层文件数、当前在哪条分支 |
| `cb blob verify [--missing]` | 重新哈希比对 / 列出缺失的 blob |
| `cb blob gc [--dedup]` | 按全历史活集清理;`--dedup` 跨天硬链接合并 |
| `cb blob push / pull <remote>` | 按活集同步 blob 库 |
| `cb doctor` | 报告缺失的 blob、坏掉的不变量,并给出恢复命令 |
| `cb hook <name>` | hook 入口,不给人直接用 |

**没有** `cb commit` / `cb checkout` / `cb log` / `cb diff`。那些就是 `git commit` / `git checkout` / …。多包一层只会让人搞不清哪个是真的。

---

## 11. 边界(v2 不做什么)

- **不管内容格式。** 层里放什么文件、什么结构、怎么互相引用,是使用者的事。工具只管路径归属、分支拓扑、以及二进制的**存放位置**(内容仍然不管)。
- **不做采集。** v1 的 worker 全部作废。事实怎么进事实层——脚本、rsync、人手动 cp——都行。
- **不推给任何下游。** 没有 sink,没有 memory system,没有 HTTP。
- **不做联合挂载 / FUSE。** 视图由 `git checkout` 提供,足够了。
- **不做冲突解决。** 按设计不会有冲突;出现了就是损坏,报错而非修复。
- **不做访问控制。** 本地 hook 是认知卫生,不是安全(§12)。

---

## 12. 已知弱点

写在这里是因为它们**不是 bug,是这一版的选择**。

**① 地板是 hook 强制的,不是结构性的。** 写入面的树里就有事实层的文件,智能体够得着。`--no-verify` 能跳过 `commit-msg`,直接写 `.git/` 更是一概看不见。三道防线(`chmod` → `commit-msg` → 投影器隔离)都软,只是把"不小心"变成"故意",并保证故意的那一下扩散不到 `layer/facts`。

**② blob 的字节不受分层保护。** 软链是被 git 跟踪的普通路径,受分层规则管;但它指向的字节在 `blob/` 里,是被忽略的,git 全程看不见。一张作为事实的截图可以被悄悄换掉,而 I1–I4 全部照过。

好在内容寻址在这里回本:sha256 就在路径里,**重新哈希一比就知道**。所以 `cb blob verify`(即 I5)不是可选功能,是必需品。

**③ 写入必须串行化。** 只有一条写入面,而同一个分支不能在两个 worktree 里同时 checkout。采集脚本在后台写事实、智能体同时干活,两者抢同一条分支,需要 `cb` 加一把提交锁。这是单写入面换来一致性的直接代价,没有免费的解。

**④ 规则散在每个 clone 里。** `core.hooksPath` 缓解了大部分,但 `git config` 那一步仍是每个 clone 一次。

**⑤ 仓库不再自足。** `git clone` 只带走软链不带走字节(实测 clone 出来 188 KB、断链)。`git clean -xdf` 会删光 `blob/`。两者都靠 `cb blob pull` 恢复,`cb doctor` 负责把话说清楚,而不是让人对着一堆断链猜。

### 要硬边界,只有两条路,都在 git 之外

- **uid 边界。** 事实文件与 blob 属于 uid A、模式 444,智能体跑 uid B。它不是所有者,`chmod` 是内核拒绝。目录加 sticky 位(`1775`,`/tmp` 那套),它删不掉不属于自己的文件,但仍能在同一目录里新建自己的——**交错布局不受影响**。这条和本设计完全兼容,是需要防对抗时的推荐方案。
- **服务端 `pre-receive`**,见 §13。

**M5 之前,这套东西的保证强度是"防手滑,不防对抗"。**用在自己驱动的 harness 上够用;要跑不受信任的智能体,配 uid 边界或等 M6。

---

## 13. 服务端:留着的那条路

规则同源、一份实现,只换执行点。`pre-receive` 相比本地多三样东西:

1. **ref 级授权。** 直接说"`stack/beliefs` 只有这个身份能推",而不是逐个路径审查内容。**能授权就别去审查**——审查总有你没想到的绕法,授权是结构性的。
2. **看得见整个 push 的每个 commit**,补上"先越界再改回来"那个漏洞(本地 hook 只看得见当次提交)。
3. **规则只有一份。**

而且**投影和重算可以整个挪到服务端**:`post-receive` 里做 §6 的两个树运算,服务器成为不变量的唯一维护者,客户端彻底不用管,§12 ③ 的提交锁也变成服务端的串行化。

成本比想象的低:`pre-receive` 只要接收方是个 bare 仓库就会跑,**本机一个目录就行**,不需要守护进程。要真正的信任边界才需要进程边界——bare 仓库换个 uid + `git-shell`,零长驻进程。

---

## 14. 里程碑

- **M1 骨架** — `cb init` 建 `layer/*` + `stack/*`、写 `collectbase.toml`、装 hook;归属推导;`cb check`(I1–I4);`cb owner`。仓库能立起来,不变量能验证。
- **M2 拦截** — `commit-msg` 三条校验 + `post-checkout` 的 chmod。地板生效。
- **M3 传播** — `post-commit` → `cb sync`(投影 + 重算)+ 提交锁。写入面和派生分支自动保持一致。
- **M4 blob** — `pre-commit` 转换 + 孤儿定点清除 + `cb blob verify/gc/push/pull`;I5 进 `cb check`。
- **M5 手感** — `cb status` / `cb layers` / `cb doctor`,错误信息说人话(拒绝时告诉它"这个路径归 facts,你要表达异议就在自己层里另写一份并引用它")。
- **M6(之后)** — 服务端 `pre-receive` + ref 级授权,把 §12 ① 补上。

---

## 15. 待定

- **supersede 语义。** 上层想说"下层那条过时了",在没有覆盖和删除的世界里怎么表达?靠约定的引用(新文件里写明 supersedes 谁),还是读的时候按层高排优先级?不填的话上层会越积越多陈旧结论。M5 之前得有个说法,哪怕只是一条约定。
- **事实层是否 append-only。** 现在只保证"上层动不了",没保证"事实层自己不能删"。要不要禁止事实层的删除与修改?
- **层的增删。** 中途在 1 和 2 之间插一层,意味着上面所有 `stack/*` 要重建。支持,还是明确不支持、只能重建仓库?
- **提交锁的形态。** §12 ③,以及采集脚本被阻塞时的行为。
- **规模。** `ls-tree` 拼索引在几万文件时的耗时;每次提交都要重算 N-1 条 stack 的并集树,提交频繁时可能需要增量化(只把改动的路径 patch 进上一棵树)。
- **blob 的日期与"事实发生时间"的关系。** 现在用添加时刻;采集三个月前的会话时,截图会落在今天的日期下。要不要允许声明一个逻辑日期?
- **软链在 Windows 上**需要开发者模式或管理员权限。当前只面向 Linux harness。
