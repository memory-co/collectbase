# `layers` 锚定、init、hook、以及别的分支

> 三件事:**`layers` 文件是唯一锚定**;**`cb init` 面向已有仓库**;**别的分支允许存在,但内容进不来**。
>
> 主设计见 [`../DESIGN.md`](../DESIGN.md);拓扑见 [`branch-topology.md`](branch-topology.md);blob 见 [`blob-store.md`](blob-store.md)。
>
> 文中"实测"来自真实执行(git 2.39.5),脚本 [`exp-branches.sh`](exp-branches.sh)。

命令只有两条:`cb init` 和 `cb check`。hook 不是 `cb` 的子命令(§3)。其余一切用 git —— 读的 git 已经有了,写的由 hook 自动发生。

---

## 1. `layers`:唯一锚定

仓库根目录一个纯文本文件,**一行一层,自下而上**:

```
# collectbase layers — 顺序即层序,第一行是事实层
facts
notes
beliefs
```

**所有东西都从它推出来**,没有第二处真相:

| 推出什么 | 怎么推 |
|---|---|
| 有哪些层 | 文件的非注释行 |
| 层序 | 行号 |
| 分支名 | `layer/<名>`、`stack/<名>` |
| 写入面 | `stack/<最后一行>` |
| 起始点位 | **首次引入这个文件的那个提交**(`git log --diff-filter=A -- layers`) |
| 提交信息的合法标签 | `[<任一行>]` |

层名受 `[a-z][a-z0-9_-]*` 约束——它同时进分支名和提交信息。

### 它归最底层

于是层的定义本身也是智能体够不着的东西:改 `layers` 需要一个 `[facts]` 提交,而它只能提交 `[beliefs]`。它没法给自己加一层、改层序、或者把事实层从栈里摘掉。

### 起始点位:规则从这里开始生效

**这个文件之前的历史不受任何约束。**已有仓库里那些没有 `[层名]` 的旧提交、混在一起的二进制、任意的分支结构,全部原样保留,不改写。`cb check` 只校验起始点位之后的部分。

这是"面向已有仓库"的关键:collectbase 不接管你的过去,它从某个提交开始接管未来。

### 改动 `layers` 时会发生什么

`post-commit` 检测到本次提交动了 `layers`,按情况调整分支集合:

| 改动 | 行为 |
|---|---|
| **顶部追加一层** | 建 `layer/<新>`(空树孤儿提交)+ `stack/<新>`(树 = 当前写入面的树,单亲接在当前写入面上)。**写入面随之上移**,提示用户 `git checkout stack/<新>` |
| **中间插一层** | 建 `layer/<新>`(空)+ `stack/<新>`。更长的 stack 的树不变(新层是空的),只是多一条读视图,很便宜 |
| **删一层** | 该层非空 → 拒绝。空 → 删掉那两条分支 |
| **改名 / 调序** | **拒绝。**改名会让历史里的 `[旧名]` 全部失真;调序等于重新划分归属 |

---

## 2. `cb init`:面向已有仓库

**默认场景就是"已经有一个 git 仓库,里面有东西"。**空仓库是它的退化情形。

### 前置检查(任一不满足就停,不做部分改动)

```
✗ 工作区有未提交的改动        → 先提交或 stash
✗ HEAD 游离                   → 先 checkout 一个分支
✗ 已存在 layers 文件          → 已初始化过,走"补配置"路径(见末尾)
✗ 已存在 layer/* 或 stack/* 分支且与将要建的重名
✗ 层名不合法 / 有重复 / 少于两层
```

### 八步

以 `cb init --layers facts,notes,beliefs` 在一个已有内容、当前在 `main` 的仓库里为例:

**① 确定起始点位** = 当前 HEAD。空仓库则先建一个空树根提交。

**② 决定既有内容的归属:全部划归最底层。**

已经存在的东西就是"给定的"——这正是事实层的定义。也是唯一能让不变量在 init 当场成立的划分:其余层为空,于是 `union(layer/*) == layer/facts` 的树 `== stack/*` 的树。

**③ 写 `layers` 和 hook,提交到当前分支。**

```
main:  … ── e4f1a2 ── [facts] collectbase: init      ← 起始点位
                        + layers
                        + .collectbase/hooks/{pre-commit,commit-msg,post-commit,
                                              post-checkout,reference-transaction}
```

提交信息带 `[facts]` 标签,让起始点位本身就符合规矩。

**④ `layer/<底层>` 直接指向起始点位**,不新建提交。

于是**仓库既有的全部历史,原样成为事实层的历史**。`git log layer/facts` 能看到 collectbase 接管之前发生过的一切。这是这个设计最省事的一处:不需要迁移,不需要重写,一条 ref 就位。

**⑤ 其余各层建空树孤儿根提交。**

```sh
empty=$(git hash-object -t tree /dev/null)
git update-ref refs/heads/layer/notes   "$(git commit-tree $empty -m '[cb] init layer/notes')"
git update-ref refs/heads/layer/beliefs "$(git commit-tree $empty -m '[cb] init layer/beliefs')"
```

**⑥ 各条 `stack/*` 也指向起始点位。**

此刻并集树 == 底层树,所以 `stack/notes`、`stack/beliefs`、`layer/facts`、`main` 四条 ref 指向**同一个提交**,不需要任何额外对象。

整个 init 只新建了 **N-1 个空孤儿提交**,其余全是 ref。

**⑦ 装 hook —— 必须最后做。**

```sh
git config core.hooksPath .collectbase/hooks
```

顺序不能反:`reference-transaction` 一旦生效,上面第 ④⑤⑥ 步的 `update-ref` 会被它自己拦住(§4)。

**⑧ 切到写入面** `stack/beliefs`,打一遍 chmod(非顶层路径 444)。

`main` 留着不动 —— 它从此是一条"其他分支"(§4),指向起始点位,不再前进。

### init 之后

```
$ cb init --layers facts,notes,beliefs
  起始点位  e4f1a2 → 4c81be  [facts] collectbase: init
  既有 352 个文件全部划归 [facts]

  layer/facts     → 4c81be   (复用起始点位,既有历史即事实层历史)
  layer/notes     → 空树孤儿
  layer/beliefs   → 空树孤儿
  stack/notes     → 4c81be
  stack/beliefs   → 4c81be   ← 写入面(已切过去)
  main            → 4c81be   保留不动,此后视作普通分支

  hook 已装,core.hooksPath = .collectbase/hooks
  提交时以 [facts] / [notes] / [beliefs] 开头声明所属层。
```

### 两条不做的

- **不回溯转换二进制。** 历史里已有的大文件留在 git 对象库里,改不了(要改就是重写历史)。blob 机制**只对起始点位之后的新提交生效**。想清一次旧账,那是 `git filter-repo` 的活,collectbase 不碰。
- **不动 remote、tag、其他分支。**

### clone 之后要再跑一次

`core.hooksPath` 是本地配置,不随 clone 走。这时 `cb init` 检测到 `layers` 已存在、分支已齐,**只补 `git config` 并切到写入面**,不重建任何东西。这是 git 的安全设计,绕不过,也不该绕。

---

## 3. hook 就是 python 脚本

collectbase 用 pip 分发,所以 hook 直接调 python,**不搞 `cb hook <name>` 这种转一道的东西**——用户打开 hook 文件应该一眼看懂发生了什么。

`.collectbase/hooks/commit-msg`:

```python
#!/usr/bin/env python3
import sys
from collectbase.hooks import commit_msg
raise SystemExit(commit_msg(sys.argv[1:]))
```

五个 hook,五个同构的小文件。它们由**最底层跟踪**,是仓库内容的一部分,clone 出来就在。

| hook | 干什么 | 出处 |
|---|---|---|
| `pre-commit` | 二进制转 blob + 软链;记下将被孤立的 OID | [blob-store §2](blob-store.md) |
| `commit-msg` | 三条校验,快速反馈(§4);可被 `--no-verify` 跳过,不是最终防线 | [branch-topology §3](branch-topology.md) |
| `post-commit` | 投影 + 重算;补主索引;定点清孤儿;`layers` 变更时调整分支集合 | [branch-topology §4](branch-topology.md) |
| `post-checkout` | 重打 chmod:非顶层 444,顶层可写 | [DESIGN §5](../DESIGN.md) |
| `reference-transaction` | **真正的闸**:检查 `old..new` 里每个提交是否合规,不合规就否决整个 ref 事务(§4) | 本篇 |

**分支分三类,hook 的行为不同**(这一点之前写得自相矛盾,现在定死):

| 分支 | `commit-msg` / 守卫 |
|---|---|
| **写入面** `stack/<最长>` | 全套校验 |
| **派生分支** `layer/*`、更短的 `stack/*` | **拒绝直接提交**——它们是生成物,只能由投影/重算更新 |
| **其他分支**(`main`、`scratch`、…) | **完全放行**,collectbase 不管 |

仓库未初始化、HEAD 游离时同样直接放行。不在一个没打算用 collectbase 的场合里挡人。

### 解释器的坑

hook 文件是被跟踪的,所以**不能把 venv 的绝对路径写死在 shebang 里**——那是机器相关的,一 clone 就错。只能用 `#!/usr/bin/env python3`,代价是 git 跑 hook 时的那个 `python3` 必须能 `import collectbase`。

导入失败时的信息必须说清楚,否则用户只会看到一个莫名其妙的 traceback:

```
✗ collectbase 未安装到当前 python3(/usr/bin/python3)。
  pip install collectbase
  若装在 venv 里,请在激活 venv 的环境下使用 git,或把 venv 的 bin 加进 PATH。
```

---

## 4. 别的分支:允许存在,但内容进不来

**其他分支是允许的**,也控不住——`git checkout -b scratch` 谁都能敲。hook 在它们上面直接放行,那是自由的草稿空间。

问题是:**草稿分支上的内容,能不能绕过 `[层名]` 校验捅进写入面?**

### 先看清楚:`commit-msg` 覆盖不了

在写入面上对一条无标签的 `other` 分支执行各种操作,看 `commit-msg` 是否被调用:

| 操作 | 跑了哪些 hook | 结果 |
|---|---|---|
| `git commit` | pre-commit, **commit-msg**, post-commit | ✅ 受控 |
| `git merge --no-ff other` | pre-merge-commit, **commit-msg**, post-merge | ✅ 可拒(还多一个 `pre-merge-commit` 可用) |
| `git merge other`(可 FF) | 只有 post-merge | ❌ **写入面被静默移到未校验的提交** |
| `git cherry-pick <c>` | 只有 post-commit | ❌ **无标签的提交直接进了写入面** |
| `git rebase other` | post-checkout, post-commit, post-rewrite | ❌ **写入面被整个重写** |
| `git reset --hard other` | **一个都没有** | ❌ **写入面被移走** |

`commit-msg` 只覆盖"新写一个提交"这一条路。**凡是移动 ref 而不新建提交内容的操作,它全看不见。**

### 解法:检查内容,不检查来源

关键的转念:**不要问"这次更新是谁发起的",要问"落进去的每个提交合不合规"。**

`reference-transaction`(git 2.28+)在**每一次 ref 更新**的 `prepared` 阶段触发,并且**能否决整个事务**。它拿得到 `old` 和 `new`,而此时提交对象已经存在,所以可以直接把 `old..new` 走一遍:

```
对 refs/heads/stack/<最长> 的更新 old → new:

  ① old 必须是 new 的祖先                  否则拒绝(rebase / reset / force 改写已有历史)
  ② old..new 里不能有 merge 节点            否则拒绝
  ③ old..new 里的每个提交:
       - 信息以合法的 [层名] 开头            否则拒绝
       - 改动的路径不属于别的层              否则拒绝,并指出归谁
```

**没有令牌,没有白名单,不关心你用的是哪条 git 命令。**合规就进,不合规就退,`git` 自己会说 `fatal: ref updates aborted by hook` 并把 ref 原样留在那里。

### 实测:七种情况

| 操作 | 结果 |
|---|---|
| `git commit -m "[facts] 正常提交"` | ✅ 放行 |
| `git commit --no-verify -m "偷偷提交"` | ✗ **拒绝**:没有 `[层名]` 前缀 |
| `git commit --no-verify -m "[notes] 顺手改事实"` | ✗ **拒绝**:`project/a.md` 属于层 `[facts]` |
| `git merge other`(FF,带无标签提交) | ✗ 拒绝:那个提交没有 `[层名]` |
| `git merge --no-ff other` | ✗ 拒绝:新增范围里有 merge 节点 |
| `git reset --hard other` | ✗ 拒绝 |
| `git cherry-pick <无标签提交>` | ✗ 拒绝 |
| **`git cherry-pick <合规的 [notes] 提交>`** | ✅ **放行** |

每一次拒绝之后写入面都纹丝不动。

最后两行是这个改法的价值所在:**判据是内容,所以合规的 cherry-pick 自然就该放行**,不需要为它开特例;而不合规的东西,无论走哪条 git 命令都进不来。

### 守卫本身的五条规则(自查补出来的,少一条就出事)

前四条是实测撞出来的,不是推演。

**① 删除必须放行,否则 `git gc` 直接坏掉。**

`git pack-refs` 把 loose ref 迁进 `packed-refs` 时,会呈现为一次 `new = 0000…` 的**删除**事务。守卫若对它做 FF 检查,`git gc` 会报:

```
fatal: ref updates aborted by hook
fatal: failed to run pack-refs
```

所以 `new` 全零时直接放行。代价是 `git branch -D stack/…` 也放行了——可接受:内容都在 `layer/*` 里,reflog 也在,`cb check` 会立刻报出来。

**② `layers` 必须从 `old` 读,不能从 `new` 读。**

从 `new` 读等于让提交**给自己发证**:一个提交可以同时往 `layers` 里加一行 `evil`、又用 `[evil]` 当自己的标签,守卫查 `new:layers` 时那一行已经在了。实测确认这个洞真实存在,写入面被 `[evil] 我自己给自己发的证` 推进了一格。

改成从 `old`(更新前的状态)读之后:

```
✗ 未知层 [evil](按更新前的 layers 判定)
fatal: ref updates aborted by hook
```

于是加层必须是**两次提交**:先一个 `[facts]` 提交改 `layers`,再用新层。这正好也是想要的——拓扑变更独立成一次提交,可审计。

顺带两条:改 `layers` 的提交**只应改 `layers`**(别和内容混在一起),**删除 `layers` 文件要拒绝**。

**③ 写入面有未投影的提交时,拒收新提交。**

守卫靠 `layer/*` 的树判断路径归属,而 `layer/*` 是 `post-commit` 更新的。如果投影没跑完,一个刚进事实层的新路径还不在 `layer/facts` 里,下一个 `[beliefs]` 提交就能把它据为己有——归属判据在那一瞬间是失真的。

规则:**写入面领先于投影时,拒绝接受新提交**,先把投影补齐(下次 `post-commit` 会按 `Cb-Stack` trailer 为界自动补)。

**④ `120000` 条目的目标必须落在 `blob/` 内。**

blob 机制假定所有软链都指向 `blob/`,但上层完全可以提交一个指向别处的软链。实测 `project/leak.txt -> /etc/hostname` 顺利进入了仓库:

```
120000 blob 48980ad…    project/leak.txt
→ /etc/hostname
```

所以守卫要求:软链目标必须是**相对路径**,且解析后**落在仓库内的 `blob/` 目录下**。其余一律拒绝。

**⑤ 路径比对要大小写折叠。**

大小写不敏感的文件系统上,`Facts/a.md` 和 `facts/a.md` 是两个 git 路径、同一个磁盘文件。归属比对不折叠的话,上层可以用改大小写的方式"新建"一个实际会覆盖下层文件的路径。

### 顺带堵上了 `--no-verify`

`--no-verify` 跳过的是 `pre-commit` 和 `commit-msg`,**它不是 `reference-transaction` 的开关**——那个钩子照跑。上表第二、三行就是实测:带着 `--no-verify` 提交一个无标签的、或者改了事实层文件的提交,一样被拒。

这把主设计 §13 ① 那条弱点降了一级:**在 git 命令这一层,地板不再是软的。**剩下的绕法只有"直接手写 `.git/refs/…` 或 packed-refs"和"把 `core.hooksPath` 拆掉"——那是拆机制,不是绕机制,性质不同。

### `commit-msg` 降级成 UX

同一条规则现在有两个执行点:

- **`commit-msg`** —— 提交对象还没建的时候就拒绝,给一段好读的、带替代动作的错误信息(§6)。快,友好,但可被 `--no-verify` 跳过。
- **`reference-transaction`** —— 真正的闸。跳不过,但它拒绝的时候提交对象已经建好了(悬空,等 gc),错误信息也更机械。

**两者跑同一条规则,一份实现,两个执行点。**前者为体验,后者为保证。

### 派生分支同理

`layer/*` 和更短的 `stack/*` 也归 `reference-transaction` 管,规则换成"必须是一次正确的投影 / 重算":新提交的树要等于按当前 `layer/*` 算出的过滤树 / 并集树,且 `layer/*` 的提交要带 `Cb-Stack` trailer 指回写入面。同样是内容检查,同样不需要令牌。

### 于是"其他分支"的规矩

- **随便建、随便提交、随便 rebase**,collectbase 完全不管。
- **想把成果拿进来**,提交本身合规就行:

  ```sh
  git cherry-pick <那个 [beliefs] 提交>       # 合规就直接过
  ```

  不合规的话,取文件重提:

  ```sh
  git checkout scratch -- path/to/file
  git commit -m "[beliefs] …"
  ```

### 三点要照实说

1. **手写 `.git/refs/…` 绕得过。** `reference-transaction` 只在 git 自己的 ref 事务里触发。这是剩下的洞,见主设计 §13。
2. **`git fetch` / `git pull` 更新 `stack/*` 也会走这套检查** —— 远端来的提交同样要合规。单机场景下正确;多 clone 协作时,派生分支的检查要重新设计(远端的 `layer/*` 未必和本地一致)。
3. **这个钩子被调用得非常频繁**,实现必须极快:先做 ref 前缀匹配,不匹配立刻退出;`old..new` 通常只有一个提交,别在里面做全库扫描。

---

## 5. `cb check`

唯一 git 做不到的检查是 **I5:blob 完整性**——软链受分层保护,但它指向的字节在 `blob/` 里,git 全程看不见,一张作为事实的截图可以被悄悄换掉而 I1–I4 全部照过。sha256 就在路径里,重新哈希一比就知道。**这是必需项,不是诊断工具。**顺手把 I1–I4 也验了,反正代码都在。

**只校验起始点位之后的部分**(§1)。之前的历史不受约束。

```
$ cb check
  起始点位  4c81be  [facts] collectbase: init   (2026-08-14)
  I1 不相交     ✓  三层共 412 个路径,无重叠
  I2 一致       ✓  stack/* 的树与并集逐字相同
  I3 线性       ✓  无 merge 节点;全部分支 fast-forward
  I4 对应       ✓  layer/* 的 87 个提交都有 Cb-Stack trailer
  I5 blob       ✗  2 个缺失、1 个哈希不匹配、1 条逃逸软链
                   缺失  blob/2026/07/23/f1a4f2…ac.png  ← layer/facts
                   篡改  blob/2026/07/19/6dd875…9d.png  ← layer/facts
                   逃逸  project/leak.txt -> /etc/hostname  ← layer/notes
                   缺失的从别处拉回来:rsync -a host:path/blob/ blob/
                   哈希不匹配意味着有人绕过机制改了字节,不自动处理
  exit 3
```

不提供 `--fix`。能自动修的只有"缺文件"一种,而那就是一条 rsync;哈希不匹配是现场,自动"修复"只会掩盖它。

退出码:`0` 通过,`1` 提交被拒(hook 用),`3` 不变量损坏,`4` 未初始化。

---

## 6. 错误信息是智能体的 UI

这一节不是文档规范,是功能需求。

智能体平时敲的是 git,它和 collectbase 的接触面**几乎只有 hook 的拒绝信息**。那几行字是它唯一能学到"这里的规矩"的地方。所以:**不只说"不行",要说"该怎么做"。**

```
✗ 拒绝提交。

  project/api.md 属于层 [facts],你声明的是 [beliefs]。
  事实层是只读的——这是设计,不是权限配置错误。

  你想表达"这个事实的记录有问题",正确做法是在自己层里另写一份并引用它:

      echo "…" > project/api-review.md
      git commit -m "[beliefs] 对 project/api.md 的存疑"

  本次提交未产生任何改动,工作区保持原样。
```

三条要求:

1. **说清为什么,并点明这是设计而非故障。** 否则智能体会去"修"权限、`chmod`、重试——把手滑变成对抗。
2. **给出可执行的替代动作**,带具体命令。
3. **说明当前状态**("未产生任何改动"),让它不必猜要不要回滚。

`reference-transaction` 的拒绝信息尤其要写好:它拦下的是 `merge` / `reset` / `rebase` / `cherry-pick`,而这些操作被拒时用户很可能一头雾水,必须直接给出 §4 末尾那条 `git checkout <branch> -- <path>` 的 recipe。

---

## 7. 待定

- **多 clone 协作。** `fetch`/`pull` 对 `stack/*` 的更新走同一套检查,单机下正确;多 clone 时派生分支的检查要重新设计(远端的 `layer/*` 未必和本地一致),多半要和服务端 `pre-receive`(主设计 §14)一起考虑。
- **submodule(`160000` 条目)完全没规定。** 归属怎么算、算不算二进制、`cb check` 怎么验,都还没想。
- **投影的并发锁。** `post-commit` 里的投影 + 重算要串行,锁放哪、等多久、超时怎么办。
- **`layers` 顶部追加一层时写入面上移**,用户/智能体正停在旧写入面上。怎么提示、要不要自动 checkout。
- **多仓库共享 blob 库。**
- **`cb blob gc` 的时机。** 手动,还是 `post-commit` 按阈值触发?活集要遍历 `git rev-list --all` 的全部树,不便宜。
