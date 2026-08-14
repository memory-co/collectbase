# CLI 与 hook:逻辑在钩子里,`cb` 只有三条命令

> 接口是 Git。真正的机制全部跑在 git hook 里,人和智能体敲的是 `git checkout` / `git commit` / `git log`。
>
> 主设计见 [`../DESIGN.md`](../DESIGN.md);拓扑见 [`branch-topology.md`](branch-topology.md);blob 见 [`blob-store.md`](blob-store.md)。

---

## 0. 全部命令

```
cb init --layers a,b,c      建分支拓扑、写配置、装 hook          人跑,一次
cb hook <name>              hook 入口                          git 调,不给人用
cb check                    校验不变量(尤其 blob 完整性)       人跑 / CI
```

加一条按需的:`cb blob gc`,库涨大了回收空间。

就这些。**其余一切用 git。**

---

## 1. 为什么其余的都不要

初稿列了十一条命令,逐条对着 git 过一遍之后,剩下三条。砍掉的和它们的 git 等价物:

| 砍掉的 | 用什么代替 |
|---|---|
| `cb sync` | **不需要**。投影和重算由 `post-commit` 做,而 `--no-verify` 跳过的是 `pre-commit` 和 `commit-msg`,**`post-commit` 照跑**。中途崩了也不用管:钩子按 `layer/*` 上最后一个 `Cb-Stack` trailer 为界,下次提交时把落下的一并处理,自愈 |
| `cb owner <path>` | `ls -l`。下层文件是 444,本层可写 —— **chmod 就是归属的答案**,不用问 |
| `cb status` | `git status` |
| `cb layers` | `git branch --list 'layer/*'`,或 `cat collectbase.toml` |
| `cb doctor` | 和 `cb check` 重复,合并 |
| `cb blob push/pull` | `rsync -a blob/ host:path/`。"按活集传输"是伪需求——整个库 rsync 一遍更简单,增量由 rsync 自己算 |
| `cb blob reclaim` | `git prune --expire=now`(正常情况下 `post-commit` 已经定点清除了,这条只是兜底) |
| `cb watch` | 不做。`pre-commit` 转换已被实测覆盖四种提交方式,watcher 只省孤儿对象,是优化不是必需 |
| `cb hook` 之外的一切 | 逻辑本来就该在钩子里 |

规律很清楚:**凡是"读"的,git 已经有了;凡是"写"的,应该由钩子自动发生,而不是等人想起来敲一条命令。**留下来的三条恰好是 git 覆盖不到的三件事——初始化、钩子本体、以及 git 看不见的 blob。

---

## 2. `cb init --layers <名字,逗号分隔>`

自下而上,第一个是事实层。

```
$ cb init --layers facts,notes,beliefs
  建立分支
    layer/facts  layer/notes  layer/beliefs      只含本层文件
    stack/notes  stack/beliefs                   派生;stack/beliefs 是写入面
  写入 collectbase.toml                           归 [facts]
  写入 .collectbase/hooks/{pre-commit,commit-msg,post-commit,post-checkout}
  git config core.hooksPath .collectbase/hooks
  切到写入面 stack/beliefs

  下一步:提交时以 [facts] / [notes] / [beliefs] 开头声明所属层。
```

- 层名受 `[a-z][a-z0-9_-]*` 约束(同时进分支名和提交信息)。
- 最底层不建 `stack/`——`stack/facts` 与 `layer/facts` 内容和历史都相同。
- **clone 之后要再跑一次**:`core.hooksPath` 是本地配置,不随 clone 走。这时检测到分支已存在,只补配置,不重建。这是 git 的安全设计,绕不过,也不该绕。

---

## 3. `cb hook <name>`:逻辑都在这里

hook 脚本本体在 `.collectbase/hooks/`,由最底层跟踪,随仓库走。每个都是一行:

```sh
#!/bin/sh
exec cb hook commit-msg "$@"
```

| hook | 干什么 | 出处 |
|---|---|---|
| `pre-commit` | 二进制转 blob + 软链;记下将被孤立的 OID | [blob-store §2](blob-store.md) |
| `commit-msg` | 三条校验:分支是否写入面、有无 `[层名]`、声明层与改动路径是否一致 | [branch-topology §3](branch-topology.md) |
| `post-commit` | 投影到 `layer/*` + 重算更短的 `stack/*`;补主索引(部分提交时);定点清孤儿对象 | [branch-topology §4](branch-topology.md) |
| `post-checkout` | 重打 chmod:非顶层路径 444,顶层可写 | [DESIGN §5](../DESIGN.md) |

**仓库未初始化 / 当前分支不是写入面 / HEAD 游离时,所有 hook 直接放行。** 不要在一个没打算用 collectbase 的场合里挡人。

### 遇到越界提交时(`--no-verify` 进来的)

`post-commit` 的投影**停下,不照做也不回滚**:

```
✗ collectbase:停止投影。

  提交 3f8a1c2 声明 [beliefs],却改动了 project/api.md(归 [facts])。
  投影会把这个改动写进 layer/facts —— 拒绝执行。

  写入面已标记 dirty,后续提交会继续被拒。处理方式:
      git revert 3f8a1c2      撤销这次越界
```

这是整个设计里唯一需要人介入的状态,只可能由 `--no-verify` 或直接写 `.git/` 产生。

### 锁

只有一条写入面,并发写入必须串行。钩子入口在 `.git/cb.lock` 上 `flock`,默认等待;超过 5 秒打印"正在等待另一个 collectbase 操作完成",别让人以为卡死。

---

## 4. `cb check`

唯一 git 做不到的检查是 **I5:blob 完整性**——软链受分层保护,但它指向的字节在 `blob/` 里,git 全程看不见,一张作为事实的截图可以被悄悄换掉而 I1–I4 全部照过。sha256 就在路径里,重新哈希一比就知道。

**这是必需品,不是诊断工具。**顺手把 I1–I4 也验了,反正代码都在。

```
$ cb check
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

退出码:`0` 通过,`1` 提交被拒(钩子用),`3` 不变量损坏,`4` 仓库未初始化。

---

## 5. 错误信息是智能体的 UI

这一节不是文档规范,是功能需求。

智能体平时敲的是 git,它和 collectbase 的接触面**几乎只有钩子的拒绝信息**。那几行字是它唯一能学到"这里的规矩"的地方。所以规矩是:**不只说"不行",要说"该怎么做"。**

反例:

```
✗ pre-commit hook failed
✗ 拒绝:越界
```

正例:

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

---

## 6. 待定

- **`cb init` 能否在已有内容的仓库上跑。** 现在假定空仓库。已有一堆文件时,要不要提供"把现有内容整体划归事实层"的迁移路径?
- **多仓库。** 一台机器上多个 collectbase 仓库时,blob 库是否共享。
- **`cb blob gc` 的时机。** 手动跑,还是 `post-commit` 里按阈值触发?活集要遍历 `git rev-list --all` 的全部树,不便宜。
