# 分支拓扑:单写入面 + 声明式分层

> 本篇定的是 v2 最底下那层机制:**层怎么映射到分支,提交怎么落,怎么保证整个仓库里一个 merge 节点都不出现。**
>
> 主设计见 [`../DESIGN.md`](../DESIGN.md)。本篇结论会回写进主设计 §3–§6、§9、§12(清单见 §9)。
>
> 文中所有"实测"都来自真实执行(git 2.39.5),不是推演。设计经过三轮修正,被否掉的两版留在附录 A、B —— 它们各自有实测数据,值得留着,免得以后再走一遍。

---

## 0. 三个必须同时满足的要求

1. **布局自由。** layer2 要能把 `api-analysis.md` 放在 layer1 的 `api.md` **旁边**,同一个目录里。分层的价值就在这;任何"每层一个顶层目录"的方案都是把初衷卖掉,不考虑。
2. **"只看某一层"要便宜。** 得是切一下就看到,不是 diff 两个分支或者拼一个 sparse-checkout 路径表。
3. **不出现 merge 节点。** 历史要能看。

三条一起,把方案空间压得很窄。最终形态如下。

---

## 1. 结论:一条写入面,两族派生分支

```
                        写                       派生(只读)
                        ↓
stack/beliefs   ●──●──●──●──●         ← 唯一的写入面,也是全局时间线
                                       工作树 = facts ∪ notes ∪ beliefs

stack/notes     ●──●──●               ← 读视图:facts ∪ notes
layer/facts     ●──●                  ← 只有事实层自己的文件 + 自己的历史
layer/notes     ●                     ← 只有 notes 自己的
layer/beliefs   ●──●                  ← 只有 beliefs 自己的
```

- **写只发生在最长那条 `stack/*` 上。**所有人、所有层,都提交到这一条。
- **提交信息必须以 `[层名]` 开头**声明自己属于哪一层,否则拒绝接收。
- 接收之后由 `cb` **投影**到对应的 `layer/*`,并**重算**所有包含该层的更短 `stack/*`。
- 其余分支全是派生的只读视图,没人往上面提交。

于是 §0 的三条:

| 要求 | 怎么满足 |
|---|---|
| 布局自由 | 完全不受限。层的区分靠路径归属表,不靠目录结构。实测三层文件交错在同一批目录里 |
| 只看某一层 | `git checkout layer/notes`,或 `git log layer/notes` 看这一层自己的演化 |
| 无 merge 节点 | 全仓库 `git rev-list --merges --all` = **0**(实测) |

底层不需要单独的 stack 分支:`stack/facts` 和 `layer/facts` 内容与历史完全相同,只建后者。

---

## 2. 为什么写入面必须只有一条

这是本篇最关键的一步推理,值得说准。

假设允许在较短的 stack 上写——比如采集器在 `stack/facts` 上提交事实。那么更长的 `stack/beliefs` 必须跟进,只有两种跟法:

- **(a) 单亲重算**:`commit-tree $新并集树 -p $上一个beliefs节点`。没有 merge 节点,但 `stack/beliefs` 上那个节点和采集器真正的那个提交**是两个不同的 commit 对象**。同一个事件有两份记录,血缘断开,谁是权威说不清楚。
- **(b) 把 `stack/facts` 并进来**:血缘对了,但这正是 merge 节点。

要求 3 排除了 (b);(a) 的代价是**最长那条 stack 不再是时间线,而是别处事件的转述**。

收敛成一条写入面之后,这个二选一消失了:

> **每个事件有且只有一个权威 commit 对象,就在最长那条 stack 上。其余分支上的一切都是它的投影。血缘、顺序、权威,三者一致。**

`layer/*` 的历史成为写入面历史的**子序列**——`git log layer/beliefs` 恰好就是"智能体自己干过的事",按真实顺序排列,中间没有任何噪音。

代价是写入必须串行化,见 §8。

---

## 3. `[层名]` 声明

```
[facts] 采集 2026-08-15 的会话
[notes] api 的调用约束笔记
[beliefs] 推翻上一版根因判断
```

### 为什么是声明,不是推断

只靠路径推断归属("这个路径在哪个 layer 分支里")也能工作,但推断在出错时**静默路由到错的地方**。声明可以和事实交叉验证:

```
提交说 [beliefs],但改动落在 layer/facts 拥有的路径上  →  当场拒绝
```

这是推断给不了的一道检查。附带的好处:`git log --oneline stack/beliefs` 每一行自带层标注,那条全局时间线变成自描述的。实测输出:

```
* 94c7c4a [beliefs] 推翻上一版根因
* 0b1f831 [facts] 采集 8-15 日志
* 1fc8481 [beliefs] 这次故障的根因判断
* 5de0292 [notes] api 的调用约束笔记
* a06bdb2 [facts] 采集 api 文档与 8-14 日志
```

### 三个定死的细节

- **用层名,不用序号。**`[facts]` 而不是 `[layer1]`。中途插一层会让序号集体错位,历史里的旧标签全部失真;名字不会。
- **放 subject 开头,不放 trailer。** trailer 在 `--oneline` 里看不见,而"一眼看出每个提交属于哪层"是它一半的价值。
- **校验放 `commit-msg` hook,不是 `pre-commit`。** 钩子顺序是 `pre-commit → prepare-commit-msg → commit-msg`,`pre-commit` 跑的时候提交信息还不存在。这个检查需要同时看到信息和暂存区,只有 `commit-msg` 两样都有(`git diff --cached` 照常可用)。

### 校验的三条

```
1. 无 [层名] 前缀                          → 拒绝
2. 层名不在 layers 文件里                   → 拒绝
3. 改动的路径已归属于别的层                 → 拒绝,并指出归谁
```

实测拒绝信息:

```
拒绝:'project/api.md' 属于层 [facts],本次提交声明的是 [beliefs]
拒绝:提交信息必须以 [层名] 开头
```

### 一个提交只能属于一层

规则的直接后果:想"顺手把事实和笔记一起提交了"是不行的,得拆两次。

**这是特性不是限制。**一个跨层的提交,恰好就是把观测和推论搅在一起的那个动作——而整个 v2 存在的理由就是不让这两样搅在一起。

---

## 4. 接收之后:投影 + 重算

一次合法提交落在写入面上之后,`cb`(由 `post-commit` 触发)做两件事,都不产生 merge 节点:

```
提交 [notes] 落在 stack/beliefs
   ↓ 投影   → layer/notes          取子集,单亲提交
   ↓ 重算   → stack/notes          重算并集树,单亲提交
   ─ 不动   → layer/facts, layer/beliefs, 以及不含 notes 的更短 stack
```

**投影**(→ `layer/<声明层>`):

```
changed = diff(S^, S)
对每个路径 p:
    owner = 含有 p 的那个 layer 分支;没有 → 归声明层(新路径)
    owner ≠ 声明层 → 越界(§3 校验已经挡住,这里是补漏)
own    = tree(layer/<声明层>) 的路径 ∪ 本次新增的路径
tree_k = 把 tree(S) 过滤成只剩 own 里的路径
提交    = commit-tree $tree_k -p <layer/声明层的上一个 tip>
```

不是 cherry-pick。cherry-pick 会引入三方合并和潜在冲突,这里完全不需要——直接算出目标树就行。

提交信息**逐字复制**,并追加 `Cb-Stack: <sha>` trailer 指回写入面上的源提交。两个 commit 对象的 SHA 必然不同(树是子集、父亲不同),所以对应关系必须显式记录,否则两族分支会各说各话。

**重算**(→ 包含声明层的每一条更短 stack):

```
tree = union(tree(layer/L1), …, tree(layer/Lk))
提交  = commit-tree $tree -p <该 stack 的上一个 tip>
```

单亲。`[facts]` 的提交会让 `stack/notes` 前进,`[beliefs]` 的提交不会——它不包含 beliefs。

---

## 5. Plumbing:两个树运算

全部机制归结成两个树运算,都不碰 Git 的合并机制。

**并集树**(造 stack)。路径不相交,所以直接拼索引:

```sh
union_tree() {                       # 参数 = 层名,自下而上
  idx=$(mktemp -u)
  for L in "$@"; do
    git ls-tree -r "refs/heads/layer/$L" | GIT_INDEX_FILE=$idx git update-index --index-info
  done
  GIT_INDEX_FILE=$idx git write-tree
}
```

`git ls-tree -r` 的输出格式(`mode SP type SP sha TAB path`)正是 `update-index --index-info` 吃的格式,直接管道。**实测可用。**

比 `git merge-tree --write-tree` 好在三点:不挑 Git 版本;没有合并语义可自作主张;重复路径的检测变显式——`update-index` 遇到重复是后者静默覆盖前者,所以必须自己查:

```sh
dup=$(for L in "$@"; do git ls-tree -r --name-only "refs/heads/layer/$L"; done | sort | uniq -d)
[ -n "$dup" ] && die "路径冲突:$dup"
```

好处是错误信息由我们写,能说清"这个路径归 facts,换一个",而不是甩一个 merge conflict 出来。

**过滤树**(造 layer)。同一套 plumbing,把 `ls-tree` 的输出按路径白名单过一遍再喂给 `update-index`。

---

## 6. 不变量与权威

```
I1  不相交     任意两层的路径集无交集
I2  一致       tree(stack/Lk) == union(tree(layer/L1..Lk))
I3  线性       所有分支无 merge 节点,且每次前进都是 fast-forward
I4  对应       layer/* 的每个提交都有 Cb-Stack trailer 指回写入面上的源提交
```

`cb check` 校验这四条。实测 I2 在五次提交序列后仍然成立(`stack/notes` 与 `stack/beliefs` 的树 OID 与重算的并集逐字相同)。

### 权威:stack,但违规提交不放行

**`stack/<最长>` 是权威。**已接收的提交以它为准,不会被重建抹掉,工作不会丢。

但权威**不等于**投影器无条件服从。`--no-verify` 同时跳过 `pre-commit` 和 `commit-msg`,所以一个绕过校验、改了事实层文件的提交是可能进入写入面的。这时:

> 投影器**停下来报错**,把写入面标记为 dirty,既不静默照做(那会把改动写进 `layer/facts`,智能体就真的改到了事实层),也不静默回滚(那会丢工作)。此后的投影继续被拒,`cb check` 报出来,等人处理。

合法提交 → stack 说了算;非法提交 → 卡在隔离区。地板保住,异常显式。

> 这是本设计里**唯一**一处需要人介入的状态。它只可能由 `--no-verify` 或直接写 `.git/` 产生,正常使用永远走不到。

---

## 7. 实测

完整脚本走了这个序列:建事实层 → 叠 notes(文件交错在同一目录)→ 叠 beliefs → **上层存在后事实层继续前进** → beliefs 修正自己 → 两次违规尝试。

| 验证项 | 结果 |
|---|---|
| 全仓库 merge 节点数 | **0** |
| 五次提交后所有分支是否 fast-forward | **全部 ✓**(每次提交后逐分支校验) |
| I2 一致性 | ✓ `stack/notes`、`stack/beliefs` 树 OID 与重算并集相同 |
| 声明层 ≠ 改动路径归属 | ✓ 拒绝,并指出该路径归谁 |
| 缺少层标签 | ✓ 拒绝 |
| 布局交错 | ✓ 三层文件同处 `project/`、`project/log/` |
| `layer/*` 内容纯度 | ✓ 各自只含本层文件 |

最终各分支内容:

```
stack/beliefs   api-belief.md  api-notes.md  api.md  2026-08-14.jsonl  2026-08-15.jsonl  summary.md
stack/notes                    api-notes.md  api.md  2026-08-14.jsonl  2026-08-15.jsonl  summary.md
layer/facts                                  api.md  2026-08-14.jsonl  2026-08-15.jsonl
layer/notes                    api-notes.md                                              summary.md
layer/beliefs   api-belief.md
```

---

## 8. 代价与已知问题

**① 写入必须串行化。** 只有一条写入面,而同一个分支不能在两个 worktree 里同时 checkout。采集器在后台往事实层写、智能体同时干活,两者抢同一条分支。需要 `cb` 加一把提交锁。这是单写入面换来一致性的直接代价,没有免费的解。

**② 地板是 hook 强制的,不是结构性的。** 写入面的树里就有事实层的文件,智能体够得着。`--no-verify` 能绕过 `commit-msg`。防线是:`chmod a-w`(挡普通写)→ `commit-msg`(挡提交)→ 投影器隔离(挡扩散到 `layer/facts`)。三道都软。

要硬的边界只有两条路,都在 git 之外:

- **uid 边界**:事实文件属于 uid A、模式 444,智能体跑 uid B。它不是所有者,`chmod` 是内核拒绝。目录加 sticky 位(`1775`,`/tmp` 那套),它删不掉不属于自己的文件,但仍能在同一目录里新建自己的——**交错布局不受影响**。这条和本设计完全兼容,推荐给需要防对抗的场景。
- **服务端 `pre-receive`**,见主设计 §13。

**③ 层名进了提交信息。** 改层名要么接受历史里的旧标签失真,要么改写历史。可接受。

**④ `[cb] init` 引导提交。** 每条分支从一个空树根提交起步。无害,但 `git log` 底下会多一行。

---

## 9. 回写主设计的清单

- **§3 拓扑** — 整节重写为本篇 §1。不变量换成本篇 §6 的 I1–I4。
- **§4 归属** — `own(Lᵢ)` 就是 `tree(layer/Lᵢ)`,不用再做集合差;新路径归**声明的层**。
- **§5 拦截** — `pre-commit` 换成 `commit-msg`,并说明为什么(顺序问题)。overlay 和 `chmod` 的维护逻辑删掉,只留"下层文件设 444"这一条最外围的提示性防护。
- **§6 传播** — `merge-tree` 换成本篇 §5 的两个树运算;新增投影步骤。
- **§7 锚定** — 层名同时是提交信息里的标签和分支名的一部分,需要约束字符集(`[a-z][a-z0-9_-]*`)。
- **§10 CLI** — 见 [`cli.md`](cli.md):逻辑全在 hook 里,`cb` 只剩 `init` / `hook` / `check`。
- **§12 弱点** — 按本篇 §8 重写,把 uid 边界作为推荐方案写进去。
- **§14 待定** — 「归属自由度」划掉(布局自由已定为硬要求)。

---

## 10. 还没验证的

- **规模。**`ls-tree` 拼索引在几万文件时的耗时;每次提交都要重算 N-1 条 stack 的并集树,提交频繁时的开销。可能需要增量化(只改动的路径 patch 进上一棵树,而不是整棵重算)。
- **提交锁。**§8 ① 的具体形态,以及采集器被阻塞时的行为。
- **`git gc --prune` 后 stack 是否完好。**stack 的树引用各层 blob,各层有 ref,理论上不会被回收,没实测过。
- **投影失败后的恢复流程。**§6 那个隔离状态,`cb` 应该提供什么样的修复命令。

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

## 附录 B:被否掉的第二版 —— stack 携带 merge 节点

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

当时选了 A。**现在两个都不需要了**:单写入面之后,连多亲节点都不存在,全仓库 merge 节点数为 0。

A 的"确定性"仍有余温:`commit-tree` 在固定 message/author/date 下是纯函数,所以 `cb check` 可以重算**树**并逐字比对(不是比 commit OID,现在 stack 是链式的,commit OID 依赖历史)。这正是 §6 里 I2 的校验方式。
