# CLI:`cb`

> collectbase 的命令行表面。**接口是 Git**——`git checkout` / `git commit` / `git log` 是日常动作,`cb` 只补 Git 补不了的那几件事。
>
> 主设计见 [`../DESIGN.md`](../DESIGN.md);拓扑见 [`branch-topology.md`](branch-topology.md);blob 见 [`blob-store.md`](blob-store.md)。

---

## 0. 定位:`cb` 只做四件事

```
① 立起来      init                     建分支拓扑、写配置、装 hook
② 维持不变量   sync / check / doctor    投影、重算、校验、修复
③ 回答归属     owner / status / layers  这个路径归哪层、我现在在哪
④ 管 blob      blob verify/gc/push/pull
```

**没有** `cb commit` / `cb checkout` / `cb log` / `cb diff` / `cb branch`。那些就是 Git 的动词,多包一层只会让人搞不清哪个是真的。

除了 `init` 和 blob 的传输,其余命令都是**幂等**的:重复跑不会产生新状态。

---

## 1. 命令全表

| 命令 | 谁调 | 幂等 | 干什么 |
|---|---|---|---|
| `cb init --layers a,b,c` | 人 | ✗ | 建 `layer/*` + `stack/*`、写配置、装 hook |
| `cb sync` | hook / 人 | ✓ | 投影 + 重算,把派生分支追平写入面 |
| `cb check [--fix]` | 人 / CI | ✓ | 校验 I1–I5 |
| `cb owner <path>…` | 人 / 脚本 | ✓ | 这些路径分别归哪层 |
| `cb status` | 人 | ✓ | `git status` 的分层版 |
| `cb layers` | 人 | ✓ | 层、分支、文件数、当前位置 |
| `cb blob verify [--missing]` | 人 / hook | ✓ | 重新哈希比对;列出缺失 |
| `cb blob gc [--dedup] [-n]` | 人 | ✓ | 按全历史活集清理 |
| `cb blob push/pull <remote>` | 人 | ✓ | 按活集同步 blob 库 |
| `cb blob reclaim [-n]` | 人 | ✓ | 兜底回收孤儿对象 |
| `cb doctor` | 人 | ✓ | 体检 + 给出修复命令 |
| `cb watch` | 人 | — | 可选常驻,落地即转换二进制 |
| `cb hook <name>` | Git | ✓ | hook 入口,不给人直接用 |

全局开关:`--repo <path>`(默认向上找)、`--json`、`--quiet`、`--no-color`。

---

## 2. 逐条

### `cb init --layers <名字,逗号分隔> [--force]`

自下而上,第一个是事实层。

```
$ cb init --layers facts,notes,beliefs
  建立分支
    layer/facts      (空树根提交)
    layer/notes
    layer/beliefs
    stack/notes      派生
    stack/beliefs    派生 ← 写入面
  写入 collectbase.toml           归 [facts]
  写入 .collectbase/hooks/{pre-commit,commit-msg,post-commit,post-checkout}
  git config core.hooksPath .collectbase/hooks
  切到写入面 stack/beliefs

  下一步:提交时以 [facts] / [notes] / [beliefs] 开头声明所属层。
```

- 层名受 `[a-z][a-z0-9_-]*` 约束(它同时进分支名和提交信息)。
- 最底层不建 `stack/`——`stack/facts` 与 `layer/facts` 内容与历史都相同。
- 仓库已初始化过则拒绝,除非 `--force`。
- **clone 之后必须再跑一次**:`core.hooksPath` 是本地配置,不随 clone 走。这时 `cb init` 检测到分支已存在,只补配置,不重建。

### `cb sync`

投影 + 重算。正常由 `post-commit` 调用,人手动跑是为了修复"hook 没触发"的场面。

```
$ cb sync
  写入面 stack/beliefs @ 94c7c4a  [beliefs] 推翻上一版根因
    → layer/beliefs   072c665  (投影)
    → stack/notes     不动(不含 beliefs)
  已追平。
```

落后多个提交时,按顺序逐个处理写入面上尚未投影的提交(以 `layer/*` 上最后一个 `Cb-Stack` trailer 为界)。

遇到越界提交(`--no-verify` 进来的)时**停下**,不照做也不回滚:

```
$ cb sync
  ✗ 停在 stack/beliefs @ 3f8a1c2
    该提交声明 [beliefs],却改动了 project/api.md(归 [facts])
    投影会把这个改动写进 layer/facts —— 拒绝执行。

    写入面已标记 dirty。处理方式:
      git revert 3f8a1c2                 撤销这次越界,然后 cb sync
      cb sync --accept-as facts          确认这确实是事实层的改动,按 [facts] 投影
  exit 3
```

### `cb check [--fix]`

```
$ cb check
  I1 不相交        ✓  三层共 412 个路径,无重叠
  I2 一致          ✓  stack/notes、stack/beliefs 的树与并集逐字相同
  I3 线性          ✓  无 merge 节点;全部分支 fast-forward
  I4 对应          ✓  layer/* 的 87 个提交都有 Cb-Stack trailer
  I5 blob 完整     ✗  2 个缺失,1 个哈希不匹配
                      缺失  blob/2026/07/23/f1a4f2…ac.png  ← layer/facts
                      篡改  blob/2026/07/19/6dd875…9d.png  ← layer/facts
                      修复: cb blob pull <remote>
  exit 3
```

`--fix` 只处理**可安全自动修复**的两类:I2 落后(跑 `cb sync`)、I5 缺失(从已配置的 remote 拉)。I1 重叠、I3 有 merge 节点、I5 哈希不匹配,一律只报不修——那些意味着有人绕过了机制,自动"修复"只会掩盖现场。

### `cb owner <path>…`

```
$ cb owner project/api.md project/api-notes.md project/new.md
  facts     project/api.md
  notes     project/api-notes.md
  -         project/new.md        (未占用,归提交时声明的层)
```

`--json` 输出 `{"path": "...", "layer": "facts"|null}` 的数组。脚本和智能体用这个判断"我能不能写"。

### `cb status`

`git status` 的分层版:按层分组,越界的标出来。

```
$ cb status
  写入面 stack/beliefs   (工作树 = facts ∪ notes ∪ beliefs)

  [beliefs]  可写
      M  project/log/why-it-broke.md
      ?  project/log/new-hypothesis.md      新路径,提交后归 beliefs

  [facts]    只读
      M  project/api.md                     ✗ 越界:这一层你改不了

  [notes]    只读
      (无改动)

  提交时请声明单一层:git commit -m "[beliefs] …"
  一次提交只能属于一层;上面两层都有改动,需要拆开提交。
```

### `cb layers`

```
$ cb layers
  #  层        分支              文件   最后提交
  3  beliefs   layer/beliefs      12    2026-08-14  [beliefs] 推翻上一版根因
  2  notes     layer/notes        48    2026-08-13  [notes] api 的调用约束笔记
  1  facts     layer/facts       352    2026-08-14  [facts] 采集 8-15 日志

  写入面  stack/beliefs   (当前所在)
  读视图  stack/notes  layer/facts  layer/notes  layer/beliefs
```

### `cb blob …`

```
$ cb blob verify
  活集 187 个 blob(扫描全部分支的全部提交)
  ✓ 184 个哈希一致
  ✗ 2 个缺失、1 个不匹配                          → cb blob pull origin

$ cb blob gc -n
  活集 187,库中 203 → 16 个可删,释放约 1.2 GB
  (去掉 -n 真正执行)

$ cb blob gc --dedup
  跨天重复 8 组 → 硬链接合并,释放约 340 MB

$ cb blob pull origin
  缺 3 个,拉取… 完成。

$ cb blob reclaim -n
  不可达对象 1 个:f257a824… blob 8000000 字节
  (等价于 git prune -n --expire=now;正常情况下 post-commit 已定点清除)
```

### `cb doctor`

体检,给命令不给废话。

```
$ cb doctor
  ✓ 已初始化,3 层
  ✓ core.hooksPath = .collectbase/hooks
  ✗ 当前在 layer/notes —— 这是只读视图,提交会被拒绝
      git checkout stack/beliefs
  ✗ blob 库为空(0 个文件,活集需要 187 个)
      多半是 git clean -xdf 删掉了。恢复:cb blob pull origin
  ✗ I2 落后 2 个提交
      cb sync
  exit 3
```

---

## 3. hook 入口

hook 脚本本体在 `.collectbase/hooks/`,由最底层跟踪,随仓库走。每个都是一行,逻辑全在 `cb` 里:

```sh
#!/bin/sh
exec cb hook commit-msg "$@"
```

| hook | `cb` 干什么 |
|---|---|
| `pre-commit` | 二进制转 blob + 软链;记下将被孤立的 OID |
| `commit-msg` | 三条校验:分支是否写入面、有无 `[层名]`、声明层与改动路径是否一致 |
| `post-commit` | 投影 + 重算;补主索引(部分提交时);定点清孤儿对象 |
| `post-checkout` | 重打 chmod:非顶层路径 444,顶层可写 |

**当前分支不是本仓库的写入面 / 仓库未初始化 / HEAD 游离时,所有 hook 直接放行。**不要在一个没打算用 collectbase 的场合里挡人。

---

## 4. 错误信息是智能体的 UI

这一节不是文档规范,是功能需求。

智能体和 `cb` 的接触面**几乎只有 hook 的拒绝信息**——它平时敲的是 git。所以那几行字是它唯一能学到"这里的规矩"的地方。规矩是:**不只说"不行",要说"该怎么做"。**

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

1. **说清为什么**,并点明这是设计而非故障——否则智能体会去"修"权限、`chmod`、重试。
2. **给出可执行的替代动作**,带具体命令。
3. **说明当前状态**("未产生任何改动"),让它不必去猜要不要回滚。

`cb status` 里那句"一次提交只能属于一层;上面两层都有改动,需要拆开提交"同理——它是在教下一步动作。

---

## 5. 退出码与输出

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 校验不通过 / 提交被拒(hook 用这个) |
| 2 | 用法错误 |
| 3 | 不变量损坏、需要人介入 |
| 4 | 仓库未初始化 |

`--json` 让所有命令输出结构化结果,给脚本和智能体用:

```json
{"ok": false, "code": 3, "checks": [
  {"id": "I5", "ok": false, "missing": 2, "mismatched": 1,
   "remedy": "cb blob pull origin"}
]}
```

**`remedy` 字段是必需的**,理由同 §4。

---

## 6. 锁

只有一条写入面,而同一分支不能在两个 worktree 里同时 checkout,所以并发写入必须串行(主设计 §12 ③)。

`cb` 在 `.git/cb.lock` 上用 `flock`:

- `cb sync` 与所有 hook 入口持写锁。
- `cb check` / `owner` / `status` / `layers` 只读,不持锁。
- 默认等待,`--no-wait` 立刻失败。等待超过 5 秒时打印"正在等待另一个 collectbase 操作完成",别让人以为卡死。

`cb blob gc` 也要持锁——它按活集删文件,不能和正在写入的提交并行。

---

## 7. 明确不做

- **不包装 Git 的日常动词。**
- **不做交互式向导。**`init` 之外没有需要引导的东西。
- **不做 TUI。**
- **不自动 `git push`。**派生分支的推送策略是使用者的事。
- **不在 `check` 里自动修一切。**能安全自动化的只有两类(§2),其余只报不修。

---

## 8. 待定

- **`cb init` 能否在已有内容的仓库上跑。** 现在假定空仓库。已有一堆文件时,要不要提供"把现有内容整体划归事实层"的迁移路径?
- **`cb sync --accept-as <层>`** 这个逃生口会不会被滥用成常规操作。可能需要要求 `--i-know-what-im-doing` 之类的显式确认。
- **`--json` 的稳定性承诺。** 智能体会依赖它,字段改名就是破坏性变更,需要版本号。
- **`cb watch` 的形态。**inotify 还是轮询;和 `cb sync` 同进程还是两个。
- **多仓库。** 一台机器上多个 collectbase 仓库时,blob 库是否共享、`cb` 怎么定位。
