# 分支拓扑:`layer/*` 与 `stack/*`

> 本篇只解一个问题:**层分支和堆叠分支怎么组织,`stack/*` 要不要携带 merge 节点。**
>
> 主设计见 [`../DESIGN.md`](../DESIGN.md)。本篇的结论会回写进主设计的 §3–§6。
>
> 文中所有"实测"都来自真实执行(git 2.39.5),不是推演。

---

## 0. 问题从哪来

主设计初稿里,层分支自己就是堆叠的:`layer/notes` 通过 merge 包含 `layer/facts`。这带来两个问题:

1. **"我只想看 layer2 的文件"没有便宜的答案。** 得 `git diff layer/facts layer/notes`,或者 sparse-checkout 一个 own(L2) 的路径列表。都能做,但都不是"切一下就看到"。
2. **`git log` 是糊的。** 在 `layer/beliefs` 上看历史,混着事实层的提交和一堆 sync 产生的 merge 节点,看不出"这一层自己干了什么"。

一个错误的解法是强制每层一个顶层目录(`facts/` `notes/` `beliefs/`)。那样 `ls notes/` 就完事了——但它**把分层的初衷卖掉了**:分层的价值恰恰是 layer2 能把 `api-analysis.md` 放在 layer1 的 `api.md` 旁边、同一个目录里。强制顶层目录等于宣布层之间不许在结构上贴近,那不如不分层。**布局自由是硬要求。**

正确的解法是把拓扑翻过来。

---

## 1. 两类分支

```
layer/facts     A───B───C            真实分支:只含本层文件,是提交的落点
layer/notes         D───E            真实分支:只含本层文件
layer/beliefs           F───G        真实分支:只含本层文件

stack/notes     = union(layer/facts, layer/notes)                  派生
stack/beliefs   = union(layer/facts, layer/notes, layer/beliefs)   派生
```

| | `layer/*` | `stack/*` |
|---|---|---|
| 内容 | 只有本层自己的文件 | 各层文件的并集 |
| 谁写 | 人 / 智能体,`git commit` 的落点 | 没人写,`cb sync` 生成 |
| 历史 | 这一层自己的演化,干净 | 见 §3 |
| 用途 | **写** | **读**:完整视图、发布、消费 |

于是开头那两个问题一起没了:

- 只看 layer2 → `git checkout layer/notes`。
- 只看 layer2 的历史 → `git log layer/notes`,里面只有 layer2 的提交。
- 要完整视图 → `git checkout stack/beliefs`。
- 布局完全自由,三层的文件可以交错在同一批目录里。实测:

  ```
  layer/facts      project/README.md  project/api.md  project/log/2026-08-14.jsonl
  layer/notes      project/api-notes.md  project/log/summary.md
  layer/beliefs    project/api-belief.md  project/log/why-it-broke.md
  ```

### 顺带:地板变成结构性的

这不是本篇的主题,但值得记一笔。翻转之后智能体的 HEAD 停在 `layer/beliefs` 上,**这个分支的树里根本没有事实层的文件**。它不是"不被允许"改事实,是它提交的分支里没有那块地。`--no-verify` 也没用,因为要拦的动作不存在。

原设计里 HEAD 停在堆叠分支上,事实文件就在树里,只能靠 hook 拦——那道防线是纸做的。

---

## 2. `stack/*` 怎么造:不用 merge 机制

路径不相交,所以并集树可以**直接用索引拼**,完全不碰三方合并:

```sh
union_tree() {                       # 参数 = 层分支,自下而上
  idx=$(mktemp -u)
  for b in "$@"; do
    git ls-tree -r "$b" | GIT_INDEX_FILE=$idx git update-index --index-info
  done
  GIT_INDEX_FILE=$idx git write-tree
}

tree=$(union_tree layer/facts layer/notes layer/beliefs)
commit=$(git commit-tree $tree -p layer/facts -p layer/notes -p layer/beliefs -m "stack/beliefs")
git update-ref refs/heads/stack/beliefs $commit
```

`git ls-tree -r` 的输出格式(`mode SP type SP sha TAB path`)正是 `update-index --index-info` 吃的格式,直接管道。**实测可用。**

比初稿里的 `git merge-tree --write-tree` 好在三点:

- 不需要 Git ≥ 2.38。
- 没有合并语义可言,也就没有"万一它自作主张解决了冲突"的担心。
- **不相交的校验变显式**。`update-index` 遇到重复路径是后者覆盖前者,静默的,所以要自己查:

  ```sh
  dup=$(for b in "$@"; do git ls-tree -r --name-only "$b"; done | sort | uniq -d)
  [ -n "$dup" ] && die "路径冲突:$dup"
  ```

  实测:layer/notes 违规新增一个 `project/api.md`(facts 已占)后,这里准确报出 `DUPLICATE:project/api.md`。比等 merge 冲突强——错误信息是我们自己写的,能说清"这个路径归 facts,换一个"。

---

## 3. 核心问题:要不要 merge 节点

两种造法,差别只在 `commit-tree` 的 parents 里加不加**上一个 stack tip**。

### A 方案:parents 只有各层 tip,不成链

```sh
git commit-tree $tree -p layer/facts -p layer/notes -p layer/beliefs
```

`stack/beliefs` 永远只比各层 tip 高**一个**节点。每次 sync 是**重建**,不是追加。实测 graph:

```
*-.   ec62d1b stack/beliefs
|\ \
| | * 25583ff  (layer/beliefs)
| * 42ea1fa    (layer/notes)
* c5e2144      (layer/facts)
* f6c06dc
```

一眼看得出"这个视图由哪三个 tip 组成"。

### B 方案:把上一个 stack tip 也当 parent,成链

```sh
git commit-tree $tree -p $prev_stack -p layer/facts -p layer/notes -p layer/beliefs
```

实测 graph,**只 sync 了两次**就已经这样:

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
* f6c06dc
```

一天几十次 sync,一周之后这东西没法看。

### 实测对照

| | A(不成链) | B(成链) |
|---|---|---|
| 两次 sync 后 commit 数 | 6 | 7(每次 sync +1,永久累积) |
| graph 可读性 | 干净,一个节点挂三个 tip | 两次就已经缠成一团 |
| **确定性** | **同样的 tip 重建 → OID 逐字相同** | 不确定(依赖上一个 tip) |
| fast-forward | **否**,旧 stack 不是新 stack 的祖先 | 是 |

确定性是实测的:同样三个 tip、固定作者与时间,两次 `commit-tree` 得到同一个 OID `ec62d1b…`。**`stack/*` 是各层 tip 的纯函数。**

---

## 4. 结论:选 A

**`stack/*` 是构建产物,应该被当作构建产物对待——重建,而不是追加。**

理由:

1. **它没有自己的历史可言。** stack 的每一次变化都完全由某个 layer 的提交引起,而那个提交在 layer 分支里已经记录得清清楚楚。B 方案的链条记的是"各层 tip 在时刻 T 分别是什么",这个信息值不值一条永久的 merge 节点?不值——真要回溯,按时间在各层历史里取就是了。
2. **确定性把非 FF 的代价削掉了大半。** 内容没变时重建出的 OID 逐字相同,`update-ref` 是 no-op,不产生任何 churn。只有真的变了才动 ref,而那时"视图被重建了"正是想表达的语义。
3. **B 的可读性衰减是不可逆的。** 干净的 graph 是 stack 分支唯一的附加价值(内容 A 和 B 完全一样),B 把它丢了。
4. **非 FF 的实际影响面很小。** overlay 靠 `git archive` 取下层,不需要 checkout;`cb` 内部也不 checkout stack。只有"人手动 checkout 了 stack 分支然后想 pull"这一种情况会撞上,`git reset --hard` 一下即可。若以后 push 到远端,`--force-with-lease`,语义正确。

> **代价照实说**:任何人把 `stack/*` 当普通分支来跟踪、在上面提交、或者期待 `git pull` 能 FF,都会被打脸。所以 `stack/*` 应当在文档和 `cb layers` 输出里被明确标注为 **generated / do-not-commit**,`cb check` 发现 stack 分支上有非 cb 生成的提交要报错。

---

## 5. 写作层怎么读下层:overlay

翻转拓扑带来一个新问题:HEAD 停在 `layer/beliefs` 上时,下层文件不在这个分支里,可智能体需要在同一个目录树里读到它们。

办法是把下层物化成**被忽略的、非跟踪的、只读的**工作区文件:

```sh
git archive stack/notes | tar -x                        # 铺进工作区
git ls-tree -r --name-only stack/notes > .git/info/exclude   # 隐形
chmod a-w $(git ls-tree -r --name-only stack/notes)     # 只读
```

### 实测结果

| 验证项 | 结果 |
|---|---|
| 工作区是否成为完整堆叠视图 | ✅ 四个文件齐全,布局交错 |
| `git status --porcelain` | ✅ 空。overlay 完全隐形 |
| 智能体写下层文件 | ✅ `Permission denied` |
| `git add -A && git commit` 是否误收 overlay | ✅ 只提交了 `project/api-belief.md`,`layer/beliefs` 树里始终只有本层文件 |
| `chmod a-w` 是否产生假 diff | ✅ 不产生。写位不进 Git(只记可执行位) |
| `git add -f` 强行加入被忽略的下层文件 | ❌ **能加进去**(`A project/api.md`) |
| 带 overlay 切层 | ⚠️ 不报错,但留下脏东西(见下) |
| 444 是否挡得住 Git 自己的改写 | ❌ **挡不住**,且覆写后权限重置为 644 |
| `git clean -xdf` | ❌ 把 overlay 全删光 |

### 三条实测出来的必须处理项

1. **`git checkout` 会静默践踏 overlay。** 被忽略的文件 Git 直接覆盖,不像非忽略的非跟踪文件那样报 "would be overwritten"。实测从 `layer/beliefs` 切到 `layer/facts`:`project/api.md` 被 Git 用自己的版本覆盖(权限重置成 644),而 `project/api-notes.md` 作为上一层 overlay 的残留留在工作区里,还被 exclude 藏着看不见。

   → `post-checkout` **必须**先清掉旧 overlay 再按新层重铺。这不是优化,是正确性要求。

2. **444 挡不住 Git,这反而是好事。** 我在主设计 §5 里标了"待验证:Git 覆写文件时是 unlink 重建还是 `open(O_TRUNC)`"。实测:`git checkout HEAD~1 -- project/api.md` 成功覆写了 444 文件。所以**不需要"解锁 → 操作 → 上锁"的包装**,`cb sync` 更新下层内容不会被只读位挡住。代价是覆写后权限重置为 644,所以 `post-checkout` / `sync` 之后**必须重新 chmod**。

3. **`git add -f` 穿透 exclude,所以 `pre-commit` 仍要保留。** 但它的地位变了:从"唯一防线"降级成"补漏"。真正的防线是"这个分支的树里没有下层文件",add -f 顶多把一个下层路径**新增**进本层——那会在下次 `cb sync` 时被 §2 的重复路径检查抓住,而且它并没有改到事实层分支上的任何东西。

`git clean -xdf` 删光 overlay 属于可接受损失:`cb sync` 重铺即可。硬链接可以把 overlay 的磁盘开销降到接近零(下层文件本来就只读,不怕被就地改)。

---

## 6. 回写主设计的清单

- **§3 拓扑**:改成 `layer/*`(真实、只含本层)+ `stack/*`(派生、并集)。不变量重写:I2「包含」不再是分支祖先关系,而是「`stack/Lᵢ` 的树 = `⋃ layer/L₁..ᵢ` 的树」。
- **§4 归属**:`own(Lᵢ)` 直接就是 `tree(layer/Lᵢ)`,不用再做集合差。
- **§5 拦截**:大幅缩水。地板由拓扑保证,`pre-commit` 只剩两件事——挡 `add -f` 进来的下层路径,挡全局已占用的新路径。
- **§6 传播**:`merge-tree` 换成索引并集;`post-checkout` 增加"清旧 overlay → 重铺 → 重新 chmod"。
- **§9 CLI**:`cb only` 不需要了(`git checkout layer/x` 就是)。
- **§12 弱点**:删掉大半。"`--no-verify` 就能改事实"不再成立。
- **§14 待定**:「归属自由度」那条划掉——布局自由是硬要求,已定。

---

## 7. 还没验证的

- **规模**。`git ls-tree -r` 拼索引在几万个文件时的耗时;overlay 铺开的 IO;硬链接方案是否可行。
- **`stack/*` 的 GC 安全性**。stack 的树引用各层的 blob,而各层有 ref,所以不该被回收——但没实测过 `git gc --prune` 之后 stack 是否完好。
- **多工作区**。`git worktree` 下 `.git/info/exclude` 是共享的(在主 `.git` 里),每个 worktree 停在不同层时 exclude 会打架。需要改用 `$GIT_DIR/worktrees/<name>/info/exclude` 或 `core.excludesFile` 按 worktree 配。**这个必须在 M2 前搞清楚**,否则"一个 harness 开多个层的工作区"直接不成立。
