# 分支拓扑:权威在 layer/*,stack 由 merge 得到

> 本篇定的是 v2 最底下那层机制:**层怎么映射到分支,提交落在哪,stack 怎么来。**
>
> 主设计见 [`../DESIGN.md`](../DESIGN.md)。文中所有"实测"都来自真实执行(git 2.39.5),不是推演。
>
> 设计经过四轮修正,被否掉的三版留在附录 —— 它们各自有实测数据,值得留着,免得以后再走一遍。

---

## 0. 三个必须同时满足的要求

1. **布局自由。** layer2 要能把 `api-analysis.md` 放在 layer1 的 `api.md` **旁边**,同一个目录里。分层的价值就在这;任何"每层一个顶层目录"的方案都是把初衷卖掉。
2. **"只看某一层"要便宜。** 切一下就看到。
3. **权威提交的 SHA 不变。** 落在权威分支上的那个提交对象,必须**原样**出现在合并视图里——不是复制一份。

第 3 条是最后一轮加进来的,而它把方案空间压到只剩一个。

---

## 1. 结论

```
                 ┌── layer/facts     ●──●──●     权威。只放这一层的东西
始祖 ●───────────┼── layer/notes     ●──●        线性、无 merge、必须 fast-forward
(写 layers 的     └── layer/beliefs   ●──●──●
 那个提交)
                    stack           ●──●──●──●  合并视图。全是 merge
                                                 树由 git 算;每个 merge 挂着那次
                                                 权威提交本身,信息逐字相同
```

- **`layer/<name>` 是唯一的权威。**
- **`stack` 是构建产物。** 坏了 `cb rebuild` 就行,原料是权威分支。
- **`stack` 名字固定。** 加一层不会让它改名。
- **所有层分支都从始祖出发**(§2)。

---

## 2. 所有层都从始祖出发

`cb init` 写 `layers` 的那个提交就是**始祖**,所有 `layer/*` 和 `stack` 都指向它。整个 init 只造这一个提交,其余全是 ref —— 没有孤儿分支,没有 octopus merge。

成立的原因是:**共同祖先在那儿,merge 的基就有了。** 各层此后各加各的文件,是相对这个基的不相交改动;git 算并集时,那份共同的基是**基**而不是"重复",不会撞。

于是"这一层的东西"= **相对始祖的增量**:

```sh
git diff --name-only --diff-filter=A <始祖> layer/notes
```

归属判定、I1 的不相交检查、`cb check` 都按这个来。

> 走过一段弯路:最初让非底层各建一个**空树孤儿提交**,结果它们不是 stack 的祖先,归位时会被当成"待 merge",拿 init 的消息盖掉真正的提交。补丁是在 init 时造一个 octopus merge —— 而那整个都不需要,只要所有分支都从始祖出发。

---

## 3. `[层名]` 声明

```
[facts] 采集 2026-08-15 的会话
[notes] api 的调用约束笔记
[beliefs] 推翻上一版根因判断
```

### 为什么是声明,不是推断

只靠路径推断归属也能工作,但推断在出错时**静默路由到错的地方**。声明可以和事实交叉验证:

```
提交说 [beliefs],但改动落在 facts 拥有的路径上  →  当场拒绝
```

附带的好处:`git log --oneline --first-parent stack` 每一行自带层标注,那条时间线是自描述的。

### 三个定死的细节

- **用层名,不用序号。** 中途插一层会让序号集体错位,历史里的旧标签全部失真。
- **放 subject 开头,不放 trailer。** trailer 在 `--oneline` 里看不见。
- **校验放 `commit-msg`,不是 `pre-commit`。** 钩子顺序是 `pre-commit → prepare-commit-msg → commit-msg`,`pre-commit` 跑的时候提交信息还不存在,而这道校验需要同时看到信息和暂存区。

### 一个提交只能属于一层

想"顺手把事实和笔记一起提交了"是不行的,得拆两次。**这是特性不是限制**——一个跨层的提交,恰好就是把观测和推论搅在一起的那个动作。

---

## 4. 站在哪条分支上,结果都一样

`git add` + `git commit` 之后一定是:**改动落在 `layer/<声明的层>` 上,再 merge 进 `stack`**。

| 你站在哪 | 发生什么 |
|---|---|
| `layer/<L>` | 你那个提交**就是**权威对象,SHA 原样带进 stack,零额外操作 |
| `stack` | 你那个对象带着所有层的内容,不可能同时是这一层的权威提交 → 在 `layer/<L>` 上落一个,草稿随后退回 |

两种情况下 merge 的树都等于你提交的树,所以**工作区始终干净**。实测两个入口落点完全一致,`git status` 为空。

**唯一的例外**:站在 `layer/notes` 上却声明 `[beliefs]`。把它挪走要**非 FF 地退回 `layer/notes`**,而权威分支必须线性只进不退——所以这一种拒绝,并提示切到 `layer/beliefs` 或去 `stack` 上提交。

> 于是 `stack` 是"什么都能写"的那个工作区:看得见所有层,声明哪层都行。智能体读着事实写结论就待在这里。

### 为什么"落"不是 cherry-pick

站在 `layer/<L>` 上时,**什么都不用做**——你的提交对象已经在那儿了。只有站在别处时才需要在 `layer/<L>` 上重造一个,而那时你那个对象本来就不是权威的(它带着所有层的内容)。

也就是说:**权威对象从不被复制。** 复制只发生在草稿身上,而草稿随后就丢了。

---

## 5. Plumbing

树全由 git 算,我们只在一处自己拼。

**合并树**(stack 前进):

```sh
git merge-tree --write-tree <stack> <layer/L>
```

各层从始祖出发,两侧改动不相交,永不冲突。**冲突就意味着 I1 被破坏了**——那不是待处理的日常,是损坏信号。

**落到权威层**(只在站在别处时):三方合并,临时索引里做,不碰工作区、不碰主索引:

```sh
GIT_INDEX_FILE=$tmp git read-tree -i -m --aggressive <C^树> <layer/L 的树> <C 的树>
GIT_INDEX_FILE=$tmp git write-tree
```

(`git merge-tree --merge-base` 要 git 2.40;这条路 2.39 就能走,实测可用。)

**并集树**:只有 `cb rebuild` 用得上,把 `git ls-tree -r` 直接喂给 `git update-index --index-info`。重复路径要自己查 `uniq -d` 并报错——好处是错误信息由我们写,能说清"这个路径归 facts,换一个"。

---

## 6. 不变量与守卫

```
I1  不相交   任意两层**相对始祖的增量**无交集(折叠大小写后)
I2  一致     tree(stack) 等于各层 tip 的合并树,且每个层 tip 都是 stack 的祖先
I3  线性     每条 layer/* 都没有 merge 节点,每次前进都是 fast-forward
I4  blob     每条 120000 软链落在 blob/ 之内、文件存在、sha256 对得上
```

守卫按分支分两种规则:

| 分支 | 规则 |
|---|---|
| **`layer/*`** | FF、无 merge 节点、`[层名]` 等于分支名、路径归属、树条目白名单 |
| **`stack`** | 每个新增提交都要带合法 `[层名]`;merge 节点内容不重查(权威那侧验过),单亲提交(你的草稿)照查内容;不要求 FF |
| 其他分支 | 完全放行 |

`stack` 不要求 FF 是因为草稿会被退回、`cb rebuild` 更是整条重造。但内容检查**必须**做——否则 `--no-verify` 就没人拦了,而 `post-commit` 的退出码 git 根本不理会。

**I2 被破坏不致命。** stack 是构建产物,`cb rebuild` 就好。I1/I3/I4 被破坏才是仓库坏了。

---

## 7. 代价与已知问题

**① 地板不是结构性的。** `stack` 的树里就有事实层的文件。防线是 `chmod a-w` → `commit-msg` → `reference-transaction`,第三道 `--no-verify` 跳不过。剩下的绕法只有直接手写 `.git/refs/` 或拆掉 `core.hooksPath`,那是拆机制不是绕机制。要硬边界用 uid(事实文件归另一个 uid、模式 444,`chmod` 是内核拒绝)。

**② 并发写入。** 两个人同时往同一条权威分支写,后一个的 FF 检查会失败(明确报错,不是丢失更新),重试即可。往不同层写互不影响——这是把权威拆成 N 条分支白捡的好处。

**③ 层名进了提交信息。** 改层名要么接受历史里的旧标签失真,要么改写历史。

---

## 8. 还没验证的

- **规模。** 几万文件时 `git merge-tree` 与 `cb rebuild` 各要多久。
- **多 clone 协作。** `fetch`/`pull` 对权威分支的更新走同一套检查,单机下正确;多 clone 时要和服务端 `pre-receive`(主设计 §14)一起考虑。

---

## 附录 A:被否掉的第一版 —— 派生 stack + overlay

**形态**:`layer/*` 是真实分支(只含本层文件,是提交落点),`stack/*` 由并集派生。智能体 HEAD 停在 `layer/beliefs` 上。

**动机很强**:那个分支的树里**根本没有事实层的文件**,智能体不是"不被允许"改事实,是它提交的分支里没有那块地。`--no-verify` 也没用,因为要拦的动作不存在。**地板是结构性的。**

**致命处**:HEAD 停在 `layer/beliefs` 上时,下层文件不在这个分支里,可智能体需要在同一个目录树里读到它们。只能把下层物化成"被忽略的非跟踪只读文件"当 overlay:

```sh
git archive stack/notes | tar -x
git ls-tree -r --name-only stack/notes > .git/info/exclude
chmod a-w $(git ls-tree -r --name-only stack/notes)
```

**实测结果**(主体是通的,坑在边上):

| 验证项 | 结果 |
|---|---|
| 工作区成为完整堆叠视图 | ✅ |
| `git status --porcelain` | ✅ 空,overlay 完全隐形 |
| 智能体写下层文件 | ✅ `Permission denied` |
| `git add -A && git commit` 是否误收 overlay | ✅ 只提交本层文件 |
| `chmod a-w` 是否产生假 diff | ✅ 不产生。Git 只记可执行位,不记写位 |
| `git add -f` 强行加入被忽略的下层文件 | ❌ **能加进去** |
| 带 overlay 切层 | ⚠️ 不报错,但**静默践踏**:被忽略的文件 Git 直接覆盖(权限重置成 644),上一层的 overlay 残留还留在工作区被 exclude 藏着 |
| 444 是否挡得住 Git 自己的改写 | ❌ **挡不住**,`git checkout HEAD~1 -- <file>` 成功覆写,且权限重置为 644 |
| `git clean -xdf` | ❌ 把 overlay 全删光 |

**否掉的理由**:overlay 是为了在纯 git 层面模拟一个本该由操作系统提供的边界而发明的,东西不精巧,实测出三个必须处理项(切层要先清再铺、每次操作后要重打 chmod、`add -f` 仍需 hook 补漏),还有一个悬而未决的(`.git/info/exclude` 在多 worktree 下是共享的,多个工作区停在不同层时会打架)。而它换来的"结构性地板"其实也只是把攻击面从"改文件"挪到"改 `.git/`"——一个能 `chmod` 的进程同样能写 `.git/`。既然硬边界终归要靠 uid,就不该为一个软边界背这么多实现复杂度。

**留下的有用事实**:上表里 `chmod a-w` 不产生假 diff、444 挡不住 Git 自己改写这两条,在当前设计里仍然成立且用得上(§8 ② 的第一道防护)。

---

## 附录 B:被否掉的第二版 —— 派生 stack 携带 merge 节点

在派生 stack 的前提下,stack 必须挂住各层 tip,于是只能是多亲节点。当时比较过两种造法:

**A 不成链**:`commit-tree $tree -p layer/facts -p layer/notes -p layer/beliefs`,stack 永远只比各层 tip 高一个节点,每次 sync 是重建。

**B 成链**:parents 里再加上一个 stack tip。

| | A(不成链) | B(成链) |
|---|---|---|
| 两次 sync 后 commit 数 | 6 | 7(每次 +1,永久累积) |
| graph 可读性 | 干净,一个节点挂三个 tip | **两次 sync 就缠成一团** |
| 确定性 | **同 tip 重建 → OID 逐字相同** | 否 |
| fast-forward | 否 | 是 |

B 的 graph 实测(只 sync 了两次):

```
*---.   4c8be1e stack/beliefs
|\ \ \
| * | | f8aaf2e facts: 3rd
| | | |
|  \ \ \
*-. \ \ \   ec62d1b stack/beliefs
|\ \ \ \ \
| |_|/ / /
|/| | / /
| | |/ /
| |/| /
| | |/
| | * 25583ff
| * 42ea1fa
* c5e2144
```

当时选了 A。**现在整条路都不走了**:stack 由真正的 merge 得到,树是 git 算的,这两种造法都不需要。

A 的"确定性"仍有余温:`cb check` 的 I2 就是重算树并逐字比对(不是比 commit OID)。
