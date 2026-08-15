# collectbase v2 — 分层记录文件系统:层即分支(设计)

> **状态:设计中(founding doc)。** v2 是一次**定位转向**。v1 的 engine / worker / sink 三角色——监听上游、增量读、归一成 session、推给 memory system——**整体作废**,不再保留。
>
> v2 的 collectbase 只干一件事:**把智能体的信息记录层做成一个分层文件系统**。最下面是事实层,智能体够不着;上层是它自己的推论,可以反复重写。底层实现是 **Git 分支**,对外接口**也是 Git**——用户和智能体敲的是 `git checkout` / `git commit` / `git log`,collectbase 提供的是让 Git 表现出分层语义的那套东西:分支拓扑 + hook。
>
> 名字仍叫 collectbase(collect = 把智能体积累的东西**收拢**成有结构的一叠),但它现在是一个**工具**,不是一个服务。
>
> v1 设计存档在 [`../v1/`](../v1/),已停止演进。

**本篇是立意 + 契约。**机制的推导过程、被否掉的方案、以及全部实测数据在子设计里:

- [`works/branch-topology.md`](works/branch-topology.md) — **分支怎么组织**。权威在 `layer/*`、`stack` 由 merge 得到、`[层名]` 声明、两个入口如何归位。附三个被否方案及其实测。
- [`works/blob-store.md`](works/blob-store.md) — **二进制怎么存**。`blob/` 外置 + 软链、`file --mime-encoding` 判据、钩子改写索引在四种提交方式下的行为、孤儿对象定点清除。
- [`works/server.md`](works/server.md) — **`cb serve`**(可选)。一组路径上的 CRUD(网页是它的客户端)+ 把 blob 传到 S3 / OSS。它没有特权,写的每个提交都过同一道闸。
- [`works/cli.md`](works/cli.md) — **锚定、init、hook、别的分支**。`layers` 文件如何成为唯一真相、`cb init` 在已有仓库上的八步、五个 hook 各干什么、以及别的分支为什么捅不进来(含 `reference-transaction` 的实测)。

---

## 0. 一句话

> **给智能体一块它够不着的地板。** 事实放最底层,只读;推论放上层,随便改。层与层之间**路径不相交**——上层永远碰不到下层已占的路径,所以没有覆盖、没有遮蔽、没有冲突,也就不需要任何合并策略。

```
git checkout stack           →   工作树 = facts ∪ notes ∪ beliefs   ← 合并视图,日常在这里干活
git checkout layer/facts     →   工作树 = 只有事实层的文件           ← 权威
git checkout layer/notes     →   工作树 = 只有 notes 的文件          ← 权威
```

**权威是 `layer/*`,`stack` 是它们 merge 出来的视图。**两边都能提交,hook 负责归位。

日常动作全是 Git 的。`cb` 只有三条命令:`init`、`check`、`rebuild`。

---

## 1. 为什么要分层

智能体的记忆会腐烂,腐烂的方式很具体:**它把自己的推论当成观测,下一轮基于那个推论再推**。三五轮之后,内容还是自洽的、还是引用得头头是道,只是跟现实脱钩了。这不是模型不够聪明,是它的工作记忆里**观测和推论长得一模一样**——都是文件,都能改,改完看不出区别。

分层就是在文件系统这一层把这条回路切断:

- **事实层**放真正发生过的东西——会话记录、命令输出、外部文档、人写下的约束。谁写它取决于场景(采集脚本、人、外部同步),但**智能体不写**。
- **上层**放智能体自己的东西:摘要、结构化的记忆、假设、结论。它可以把上一版全推翻重来,这是它该干的事。
- 上层**改不动**下层。

所以演化是有地板的:无论上层怎么迭代,重新推导的原料永远是那份没被动过的事实。

> 这不是访问控制,是**认知卫生**。目的不是防坏人,是让"我看到的"和"我以为的"在文件系统上是两种东西。防对抗要另外的手段,见 §13。

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

## 3. 拓扑:权威在 layer/*,stack 是合并视图

```
layer/facts     ●──●──●        权威。只放事实层的文件,线性,必须 fast-forward
layer/notes     ●──●
layer/beliefs   ●──●──●

stack           ●──●──●──●──●  合并视图。全是 merge,树 = 各层 tip 的并集
                 ╲  ╲  ╲  ╲ ╲   每个 merge 的信息与那次权威提交逐字相同
```

- **`layer/<name>` 是唯一的权威。** 每条只放自己这一层的文件,历史线性、无 merge、必须 FF。
- **`stack` 是构建产物。** 一串 merge 节点,树是各层 tip 的不相交并集。**并集由 git 自己算**——路径不相交,merge 天然无冲突,我们不用拼树。
- **`stack` 名字固定。** 加一层不会让它改名,所以没人需要为此切分支。

### 两个入口,hook 负责归位

```
在 stack 上提交      工作区看得见所有层 → hook 把它拆到权威层,再把 merge 节点顶上去
在 layer/<L> 上提交   本来就是权威的     → hook 把它 merge 进 stack
```

**在 stack 上提交是主路径**,因为只有那个工作区同时看得见所有层——智能体要读着事实写结论。它敲的仍然是 `git commit`;归位是 hook 的事。

### 一次改动在 stack 上只留一个节点

在 stack 上提交后,那个提交**不是**权威的:hook 把属于声明层的子集提交到 `layer/<L>`,把 stack 退回提交前,再造一个 merge 顶上去。所以 `git log --first-parent stack` 读起来就是那条时间线本身,不是"你的提交 + 一个 Merge branch"。

### 四条不变量

- **I1 不相交** — 任意两层的路径集无交集(折叠大小写后)。
- **I2 一致** — `tree(stack) == union(各层 tip 的树)`,且每个层 tip 都是 stack 的祖先。
- **I3 线性** — 每条 `layer/*` 都没有 merge 节点,每次前进都是 fast-forward。
- **I4 blob 完整** — 每条 `120000` 软链的目标是相对路径、落在 `blob/` 之内、文件存在,且内容 sha256 与路径中的哈希一致。

`cb check` 校验这四条(只看起始点位之后,见 §7)。I1/I3/I4 被破坏是仓库坏了;**I2 被破坏不致命**——stack 是构建产物,`cb rebuild` 就好,原料是权威分支,不是它自己。

---

## 4. 归属:一个路径归哪层

```
own(Lᵢ) = tree(layer/Lᵢ) 里的全部路径
```

就这么直接——`layer/*` 分支只含本层文件,它的树**就是**归属表,不用做集合差,也不用另立登记表。规则和事实是同一份数据,不可能出现"登记说归 A、实际在 B"。

**新路径归声明的层。**提交信息里的 `[层名]` 就是那个信号,不靠推断。

**归属稳定**:一个路径一旦被某层引入,永远归那层。想搬家就是下层删、上层加,两次提交,历史里看得见。

---

## 5. 拦截:一条规则,两个执行点

```
① chmod a-w              写事实层文件的那一刻就 EACCES   ← 最快的反馈
② commit-msg             提交时拒绝,信息友好             ← 为体验,--no-verify 可跳过
③ reference-transaction  ref 更新时检查落进去的内容      ← 为保证,--no-verify 跳不过
④ pre-receive            push 时同一套检查              ← 服务端,暂不做(见 §14)
```

②③ 跑的是**同一条规则、同一份实现**,区别只在执行点。

### 守卫按分支分两种规则

| 分支 | 规则 |
|---|---|
| **`layer/*`**(权威) | FF、无 merge 节点、每个提交的 `[层名]` 必须等于分支名、路径归属、树条目白名单 |
| **`stack`**(构建产物) | 每个提交都要带**合法的 `[层名]`**(merge 节点也一样),非 merge 的还要过归属与白名单;**不要求 FF**,也不禁止 merge |
| 其他分支 | 完全放行 |

`stack` 不要求 FF,是因为归位时会改写它(把你的提交换成 merge)。但它**不是不设防**:每个提交的信息都有要求。merge 节点的信息是从权威提交逐字复制来的,所以这条自然满足;**一个不满足它的节点,就说明那不是归位产生的**。

内容检查在 stack 上同样必须做——否则 `--no-verify` 就没人拦了,而 `post-commit` 的退出码 git 根本不理会。

### 为什么 ② 是 `commit-msg` 而不是 `pre-commit`

钩子顺序是 `pre-commit → prepare-commit-msg → commit-msg`。`pre-commit` 跑的时候**提交信息还不存在**,而这道校验需要同时看到信息(声明的层)和暂存区(实际改动的路径)。只有 `commit-msg` 两样都有。

`pre-commit` 另有职责:blob 转换(§8)。

### 三条校验

```
1. 在 layer/<L> 上而信息声明了别的层     → 拒绝
2. 无 [层名] 前缀 / 层名不在 layers 里    → 拒绝(层名按更新前的 layers 判定)
3. 改动的路径已归属于别的层              → 拒绝,并指出归谁
```

**一个提交只能属于一层。**这是特性不是限制——一个跨层的提交,恰好就是把观测和推论搅在一起的那个动作。

两个比对细节:**路径按折叠大小写比**;**软链只允许指向 `blob/` 之内**(§8 白名单)。

### ① post-checkout:锁住事实层

只锁**最底层**。地板要守的是事实,中间层不是信任边界,而 stack 是所有写入者共用的——人提交一条 `[notes]` 不该撞上 EACCES。站在 `layer/facts` 上时不锁:那就是来写事实的。

`layers`、`.gitignore`、`.collectbase/config.yaml` 例外:它们在最底层里,但那是**机制不是证据**,锁上就没人能加层了。

两件事让这个土办法能用,都已实测:**不产生假 diff**(git 只记可执行位);**不妨碍 git 自己干活**(实测 `git checkout HEAD~1 -- <file>` 能覆写 444 文件)。代价是覆写后权限重置为 644,所以每次 checkout / 归位之后必须重打。

---

## 6. 归位:拆到权威层,再 merge

`post-commit` 干这件事。

**在 `layer/<L>` 上提交时**,只有一步:

```
stack = commit-tree <各层 tip 的并集树> -p <stack> -p <layer/L>   信息 = 权威提交的信息
```

**在 `stack` 上提交时**,先拆再合:

```
① 取出属于声明层的子集      own = tree(layer/L) 的路径,按本次改动增删
   layer/L = commit-tree <过滤后的树> -p <layer/L>    信息逐字复制
② stack 退回到提交前         update-ref stack <你那个提交的父亲>
③ 把 merge 顶上去            见上
```

于是那次提交在 stack 上只留一个节点,而权威内容落在 `layer/L` 上。

**并集树由谁算。**merge 是 git 算的,不需要我们拼——路径不相交,三方合并没有同路径分歧。我们只在两处自己拼树:拆子集时(过滤树),和 `cb rebuild` 时(并集树)。后者用 `git ls-tree -r` 直接喂给 `git update-index --index-info`,重复路径要自己查 `uniq -d` 并报错——好处是错误信息由我们写,能说清"这个路径归 facts,换一个"。

### 归位失败时

提交信息没有合法 `[层名]`(只可能是绕过了 `commit-msg`),归位**停下报错**,提交留在原地但没有落到任何权威层上。`git commit --amend` 改完信息再试。这是设计里唯一需要人介入的状态。

---

## 7. `layers`:唯一锚定

仓库根目录一个 YAML 文件 `layers`,**一个列表,自下而上**:

```yaml
# 顺序即层序,第一项是事实层
- facts
- notes
- beliefs
```

**所有东西都从它推出来**,没有第二处真相:有哪些层、层序、权威分支名(`layer/<名>`)、提交信息的合法标签、以及**起始点位**——首次引入这个文件的那个提交。`stack` 的名字是固定的,加一层不会让它改名。

**它归最底层。**改 `layers` 需要一个 `[facts]` 提交,而智能体只能提交 `[beliefs]`。它没法给自己加一层、改层序、把事实层从栈里摘掉。

**起始点位之前的历史不受任何约束。**已有仓库里那些没有层标签的旧提交、混在一起的二进制、任意的分支结构,全部原样保留、不改写;`cb check` 只校验起始点位之后的部分。collectbase 不接管你的过去,它从某个提交开始接管未来——这是"面向已有仓库"的关键。

blob 的可调项另放 `.collectbase/config.yaml`(阈值、`force_in` / `force_out`),锚定文件保持最小。

> `cb init` 在已有仓库上的完整八步、`layers` 变更时分支集合怎么调整,见 [`works/cli.md`](works/cli.md) §1–§2。

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

### 白名单:git 里只允许两种形态

blob 机制反过来定义了"什么能进 git",这份白名单同时是 `reference-transaction` 的校验规则:

```
100644 / 100755   内容不是二进制(否则本该先转成 blob 软链)
120000            目标是相对路径,且解析后落在 blob/ 之内
其余一律拒绝      160000(submodule)、任何别的模式
```

`pre-commit` 是转换器,守卫是校验器,**同一条规则的两面**。这一点很要紧:`--no-verify` / `cherry-pick` / `rebase` 都不跑 `pre-commit`,没有守卫这一侧,裸二进制就能直接进来(实测确实进去了)。

两者的二进制判据**必须是同一份实现**,否则转换器说"是文本不转"、校验器说"是二进制拒绝",这个提交就永远进不去。`file` 能读 stdin,所以守卫拿 blob OID 也能用完全相同的判断:`git cat-file blob <oid> | file -b --mime-encoding -`。

> 三条少一条就漏的实现要点(`--diff-filter` 必须含 `T`、必须改工作区、部分提交要 `post-commit` 补主索引)见 [`works/blob-store.md`](works/blob-store.md) §2。**这三条都是实测撞出来的,不是推演。**

---

## 9. 用起来是什么样

在一个**已经有内容**的仓库里:

```sh
cb init --layers facts,notes,beliefs
# 起始点位 = 当前 HEAD;既有 352 个文件全部划归 [facts];建分支、装 hook、切到 stack
# 之前的历史原样保留,不改写;main 留着不动,此后视作普通分支

# 之后一直待在 stack 上,不用再切

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

# 只看某一层 / 某一层的演化(它们是权威,不是投影)
git checkout layer/notes
git log --oneline layer/beliefs              # 只有智能体自己干过的事

# 也可以直接在权威分支上提交,hook 会把它 merge 进 stack
git checkout layer/facts
cp …/session.jsonl project/log/2026-08-15.jsonl
git add -A && git commit -m "[facts] 采集 8-15 会话"

# 全局时间线,自带层标注
git log --oneline --first-parent stack
#   * [beliefs] 推翻上一版根因
#   * [facts]   采集 8-15 日志
#   * [beliefs] 这次故障的根因判断
#   * [notes]   api 的调用约束笔记
```

---

## 10. CLI 与 hook

命令只有两条:

```
cb init --layers a,b,c      面向已有仓库:定起始点位、既有内容归最底层、建分支、装 hook
cb check                    校验 I1–I4(尤其 I4 blob 完整性),只看起始点位之后
cb rebuild                  从各层 tip 重建 stack —— 它是构建产物,这么做永远安全
cb serve                    可选:路径 CRUD + blob 出口(见 works/server.md)
```

`cb serve` 是**可选**的:不跑它整套机制照常工作(分层、校验、归位、blob 转换全在 hook 里,零常驻进程)。而且它**没有特权**——往仓库里写东西的方式和人敲 `git commit` 一模一样,被同一套 hook 校验。只要 server 有一条绕过 hook 的路径,那条路径就是地板上的洞,而它恰好是唯一可能被网络访问到的组件。

**hook 不是 `cb` 的子命令。**collectbase 用 pip 分发,hook 就是直接调 python 的小脚本,打开一眼看懂:

```python
#!/usr/bin/env python3
import sys
from collectbase.hooks import commit_msg
raise SystemExit(commit_msg(sys.argv[1:]))
```

五个 hook:`pre-commit`(blob 转换)、`commit-msg`(三条校验)、`post-commit`(归位)、`post-checkout`(chmod)、`reference-transaction`(内容守卫,见 §11)。

其余一切用 git:读的 git 已经有了(`git status` / `git log` / `git branch --list 'layer/*'`;甚至"这个路径归哪层"都不用问——下层是 444,`ls -l` 就是答案),写的由 hook 自动发生。

> 完整规格见 [`works/cli.md`](works/cli.md)。其中「错误信息是智能体的 UI」是功能需求而非文档规范——智能体和 collectbase 的接触面几乎只有 hook 的拒绝信息。

---

## 11. 其他分支:允许存在,但内容进不来

`git checkout -b scratch` 谁都能敲,也控不住,所以**其他分支是允许的**,hook 在上面直接放行,那是自由的草稿空间。

但草稿分支上的东西**不能绕过 `[层名]` 校验捅进来**。实测发现 `commit-msg` 只覆盖"新写一个提交"这一条路,四种操作里三种能绕过:

| 操作 | `commit-msg` | 结果 |
|---|---|---|
| `git commit` | ✅ | 受控 |
| `git merge --no-ff` | ✅ | 可拒 |
| `git merge`(可 FF) | ❌ | 分支被静默移到未校验的提交 |
| `git cherry-pick` | ❌ | 无标签的提交直接进来了 |
| `git rebase` | ❌ | 分支被整个重写 |
| `git reset --hard` | ❌ 一个 hook 都不跑 | 分支被移走 |

**凡是移动 ref 而不新建提交内容的操作,`commit-msg` 全看不见。**

解法是 `reference-transaction` 钩子(git 2.28+)。它在每一次 ref 更新的 `prepared` 阶段触发、**能否决整个事务**,是唯一覆盖 merge-FF / reset / rebase / cherry-pick / push 的钩子。

关键的转念是**检查内容,不检查来源**——不问"这次更新是谁发起的",只问"落进去的每个提交合不合规":

```
对 layer/* 的更新 old → new:
  ① new 全零(删除)               放行 —— 否则 git gc 会坏(见下)
  ② old 必须是 new 的祖先          否则拒绝(rebase / reset / force 改写已有历史)
  ③ old..new 里不能有 merge 节点    权威分支必须线性
  ④ old..new 里的每个提交:
       [层名] 必须等于分支名         提交到 layer/notes 就得声明 [notes]
       层名按 old:layers 判定        不是 new —— 否则提交能给自己发证
       改动路径不属于别的层          折叠大小写后比对
       每个树条目走白名单            见 §8

对 stack 的更新:
  每个提交(含 merge)都要带合法的 [层名];非 merge 的还要过归属与白名单。
  不要求 FF,也不禁止 merge —— 归位会改写它,而它本来就全是 merge。
```

没有令牌,没有白名单,不关心你用的是哪条 git 命令。实测七种情况全部符合预期,每次拒绝后受管分支纹丝不动;而**一个合规的 `[notes]` 提交被 cherry-pick 过来是放行的**——判据是内容,所以不需要为任何操作开特例。

**四条容易漏的**(都是实测撞出来的):`git pack-refs` 把 loose ref 迁进 `packed-refs` 时呈现为一次删除事务,不放行就 `fatal: failed to run pack-refs`,`git gc` 直接坏掉;`layers` 若从 `new` 读,一个提交就能**同时加层并用这层给自己发证**(实测得逞),必须从 `old` 读,于是加层天然变成两次提交;树条目不走白名单的话,`--no-verify` 跳过 `pre-commit` 就不会 blobify,**一个裸的 2 MB 二进制会直接进来**(实测确实进去了),`project/leak.txt -> /etc/hostname` 这样的逃逸软链也照收不误;`cherry-pick` 和 `rebase` 同样不跑 `pre-commit`。


### 顺带堵上了 `--no-verify`

`--no-verify` 跳过的是 `pre-commit` 和 `commit-msg`,**它不是 `reference-transaction` 的开关**。实测:带 `--no-verify` 提交一个无标签的、或者改了事实层文件的提交,一样被拒。

于是同一条规则有了两个执行点:**`commit-msg` 为体验**(提交对象还没建就拒绝,信息友好,可被跳过),**`reference-transaction` 为保证**(跳不过)。一份实现,两处执行。

### 规矩

草稿分支随便建、随便提交、随便 rebase,collectbase 完全不管。想把成果拿进来,提交本身合规就直接 `cherry-pick`;不合规就取文件重提:

```sh
git checkout scratch -- path/to/file
git commit -m "[beliefs] …"
```

> 剩下的绕法只有"直接手写 `.git/refs/…`"和"把 `core.hooksPath` 拆掉"——那是拆机制,不是绕机制。另:`git fetch`/`pull` 更新 `stack/*` 也走同一套检查,单机下正确,多 clone 协作时派生分支的检查要重新设计。

---

## 12. 边界(v2 不做什么)

- **不管内容格式,也不管文件之间的关系。** 层里放什么、什么结构、谁引用谁、哪条结论取代哪条、某个判断依赖哪些事实——**全是使用者的事**。工具只管路径归属、分支拓扑、以及二进制的存放位置。

  > 这条容易越界。"事实变了,哪些结论该复查"是个真问题,但它是**用文件系统的人**的问题,不是文件系统的问题。ext4 不替你定义哪份文档取代哪份。真需要的话,在自己层的文件里写个 front-matter 就完了,collectbase 不必知道。
- **不支持 submodule。** `160000` 条目不在 §8 的白名单里,直接拒绝。
- **不做采集。** v1 的 worker 全部作废。事实怎么进事实层——脚本、rsync、人手动 cp——都行。
- **不推给任何下游。** 没有 sink,没有 memory system,没有 HTTP。
- **不做联合挂载 / FUSE。** 视图由 `git checkout` 提供,足够了。
- **不做冲突解决。** 按设计不会有冲突;出现了就是损坏,报错而非修复。
- **不做访问控制。** 本地 hook 是认知卫生,不是安全(§13)。

---

## 13. 已知弱点

写在这里是因为它们**不是 bug,是这一版的选择**。

**① 地板不是结构性的,但也不再是纸做的。** `stack` 的树里就有事实层的文件,智能体够得着。但要把改动落进去,ref 必须更新,而 `reference-transaction` 会检查落进去的每个提交(§11)——`--no-verify` 跳不过它,`merge` / `reset` / `rebase` / `cherry-pick` 也绕不过。

剩下的绕法只有两条:**直接手写 `.git/refs/…` 或 packed-refs**(不走 git 的 ref 事务),以及**把 `core.hooksPath` 拆掉**。这两条是拆机制,不是绕机制——一个只想省事的智能体不会走到那里,一个铁了心的进程则本来就该用 §13 末尾那两种硬边界来挡。

**② blob 的字节不受分层保护。** 软链是被 git 跟踪的普通路径,受分层规则管;但它指向的字节在 `blob/` 里,是被忽略的,git 全程看不见。一张作为事实的截图可以被悄悄换掉,而 I1–I4 全部照过。

好在内容寻址在这里回本:sha256 就在路径里,**重新哈希一比就知道**。所以 `cb check` 的 I4 不是可选功能,是必需品。

**③ 并发写入。** 两个人同时往同一条权威分支写,后一个的 FF 检查会失败(明确报错,不是丢失更新),重试即可。往不同层写互不影响——这是把权威拆成 N 条分支白捡的好处。

**④ 规则散在每个 clone 里。** `core.hooksPath` 缓解了大部分,但 `git config` 那一步仍是每个 clone 一次。

**⑤ 仓库不再自足。** `git clone` 只带走软链不带走字节(实测 clone 出来 188 KB、断链)。`git clean -xdf` 会删光 `blob/`。两者都靠把库从别处 rsync 回来恢复;`cb check` 负责把话说清楚,而不是让人对着一堆断链猜。

### 要硬边界,只有两条路,都在 git 之外

- **uid 边界。** 事实文件与 blob 属于 uid A、模式 444,智能体跑 uid B。它不是所有者,`chmod` 是内核拒绝。目录加 sticky 位(`1775`,`/tmp` 那套),它删不掉不属于自己的文件,但仍能在同一目录里新建自己的——**交错布局不受影响**。这条和本设计完全兼容,是需要防对抗时的推荐方案。
- **服务端 `pre-receive`**,见 §14。

**M5 之前,这套东西的保证强度是"防手滑,不防对抗"。**用在自己驱动的 harness 上够用;要跑不受信任的智能体,配 uid 边界或等 M6。

---

## 14. 服务端:留着的那条路

规则同源、一份实现,只换执行点。`pre-receive` 相比本地多三样东西:

1. **ref 级授权。** 直接说"`layer/facts` 只有这个身份能推",而不是逐个路径审查内容。**能授权就别去审查**——审查总有你没想到的绕法,授权是结构性的。
2. **看得见整个 push 的每个 commit**,补上"先越界再改回来"那个漏洞(本地 hook 只看得见当次提交)。
3. **规则只有一份。**

而且**归位可以整个挪到服务端**:`post-receive` 里做 §6 那两步,服务器成为不变量的唯一维护者,客户端彻底不用管。

成本比想象的低:`pre-receive` 只要接收方是个 bare 仓库就会跑,**本机一个目录就行**,不需要守护进程。要真正的信任边界才需要进程边界——bare 仓库换个 uid + `git-shell`,零长驻进程。

---

## 15. 里程碑

- **M1 骨架** — `cb init` 面向已有仓库的八步(起始点位、既有内容归最底层、建分支、装 hook);`cb check`(I1–I4)。仓库能立起来,不变量能验证。
- **M2 拦截** — `reference-transaction` 的内容检查(真正的闸)+ `commit-msg` 同规则快速反馈 + `post-checkout` 的 chmod。地板生效,别的分支也进不来。
- **M3 归位** — `post-commit` 拆到权威层 + merge 进 stack,两个入口都覆盖。
- **M4 blob** — `pre-commit` 转换 + 孤儿定点清除 + `cb blob gc`;I4 进 `cb check`。
- **M5 手感** — 钩子的拒绝信息说人话:告诉它"这个路径归 facts,你要表达异议就在自己层里另写一份并引用它",带具体命令。见 [`works/cli.md`](works/cli.md) §6。
- **S1–S4(与 M4 之后并行)** — `cb serve`:blob 传到对象存储 + 网页上传入口。见 [`works/server.md`](works/server.md) §6。
- **M6(之后)** — 服务端 `pre-receive` + ref 级授权,把 §13 ① 补上。

---

## 16. 待定

- **事实层是否 append-only。** 现在只保证"上层动不了",没保证"事实层自己不能删"。要不要禁止事实层的删除与修改?
- **层的增删。** 中途在 1 和 2 之间插一层,意味着上面所有 `stack/*` 要重建。支持,还是明确不支持、只能重建仓库?
- **规模。** 几万文件时,`git merge` 与 `cb rebuild` 的并集拼装各要多久。日常路径靠 git 的 merge,应该没问题;`rebuild` 是全量的。
- **blob 的日期与"事实发生时间"的关系。** 现在用添加时刻;采集三个月前的会话时,截图会落在今天的日期下。要不要允许声明一个逻辑日期?
- **软链在 Windows 上**需要开发者模式或管理员权限。当前只面向 Linux harness。
