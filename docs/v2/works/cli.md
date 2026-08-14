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
| `commit-msg` | 三条校验(§4);通过后放行令牌 | [branch-topology §3](branch-topology.md) |
| `post-commit` | 投影 + 重算;补主索引;定点清孤儿;`layers` 变更时调整分支集合;最后收回令牌 | [branch-topology §4](branch-topology.md) |
| `post-checkout` | 重打 chmod:非顶层 444,顶层可写 | [DESIGN §5](../DESIGN.md) |
| `reference-transaction` | 否决一切未经 collectbase 的 `layer/*` `stack/*` 更新(§4) | 本篇 |

**仓库未初始化 / 当前分支不是写入面 / HEAD 游离时,所有 hook 直接放行。** 不在一个没打算用 collectbase 的场合里挡人。

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

### 实测:四条路里三条能绕过

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

### 解法:`reference-transaction`

git 2.28+ 的 `reference-transaction` 钩子在**每一次 ref 更新**时触发,并且在 `prepared` 阶段**能否决整个事务**。它是唯一一个覆盖 merge-FF / reset / rebase / push 的钩子。

```python
# reference-transaction  (prepared 阶段,stdin 逐行 "<old> <new> <ref>")
for old, new, ref in updates:
    if not ref.startswith(("refs/heads/layer/", "refs/heads/stack/")):
        continue                       # 别的分支,不管
    if old == new:
        continue                       # pack-refs 之类的无变化事务
    if token_present():
        continue                       # collectbase 自己发起的
    reject(f"{ref} 只能由 collectbase 更新")
```

**令牌协议**:`commit-msg` 校验通过后在 `$(git rev-parse --git-dir)/cb-allow` 放一个令牌;`post-commit` 在**做完投影和重算的所有 `update-ref` 之后**才收回。这样合法提交及其派生的 ref 更新都能通过,其余一律被否。

实测:

```
$ git reset --hard other
  [ref-txn] 拒绝更新 refs/heads/stack/top —— 未经 collectbase
  fatal: ref updates aborted by hook
  → 写入面 = 71f41cc  ([facts] base)          ← 没动

$ git merge other
  [ref-txn] 拒绝更新 refs/heads/stack/top —— 未经 collectbase
  fatal: ref updates aborted by hook
  → 写入面 = 71f41cc  ([facts] base)          ← 没动

$ git commit -m "[facts] 正常提交"
  [hook] pre-commit
  [hook] commit-msg 校验通过,放令牌
  [hook] post-commit 收令牌
  → 写入面 = 6253a06  ([facts] 正常提交)      ← 放行
```

`git rebase`、`git cherry-pick` 同理被拦——它们最终都要更新 `refs/heads/stack/*`。

### 于是"其他分支"的规矩很清楚

- **随便建、随便提交、随便 rebase**,collectbase 不管。
- **想把成果拿进来,只有一条路:在写入面上正常提交。**

  ```sh
  git checkout stack/beliefs
  git checkout scratch -- path/to/file      # 把文件取过来
  git commit -m "[beliefs] …"               # 走正常校验
  ```

  `cherry-pick` 不行——它不跑 `commit-msg`,会被 `reference-transaction` 拦在最后一步,而且拦下时你已经处理完冲突了,体验很差。文档和错误信息里要直接给上面这条 recipe。

### 三点要照实说

1. **令牌是文件,不是密钥。** 能写文件的进程就能伪造它。这道闸挡的是"顺手一个 `git reset` 把写入面搞坏",不是对抗——和 §12 里其余几道防线同一级别。
2. **`git fetch` / `git pull` 更新 `stack/*` 也会被拦。** 单机场景下这是对的(远端内容必须走正常路径进来)。将来做多 clone 协作时,这条规则要重新设计。
3. **`reference-transaction` 会被非常频繁地调用**,实现必须极快:先做前缀匹配,不匹配立刻退出,别在里面跑 git 命令。

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
  I5 blob       ✗  2 个缺失、1 个哈希不匹配
                   缺失  blob/2026/07/23/f1a4f2…ac.png  ← layer/facts
                   篡改  blob/2026/07/19/6dd875…9d.png  ← layer/facts
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

- **多 clone 协作。** §4 ② 那条规则(拦下 `fetch`/`pull` 对 `stack/*` 的更新)在单机下是对的,多 clone 时要重新设计——多半要和服务端 `pre-receive`(主设计 §13)一起考虑。
- **`layers` 顶部追加一层时写入面上移**,用户/智能体正停在旧写入面上。怎么提示、要不要自动 checkout。
- **多仓库共享 blob 库。**
- **`cb blob gc` 的时机。** 手动,还是 `post-commit` 按阈值触发?活集要遍历 `git rev-list --all` 的全部树,不便宜。
