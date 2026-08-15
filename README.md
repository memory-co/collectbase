# collectbase

**给智能体一块它够不着的地板。**

事实放最底层,只读;推论放上层,随便改。层就是 git 分支,层与层之间**路径不相交**——上层永远碰不到下层已占的路径,所以没有覆盖、没有遮蔽、没有冲突。

对外接口就是 **git**:`git checkout` / `git commit` / `git log`。collectbase 提供的是让 git 表现出分层语义的那套东西——分支拓扑 + hook。

```bash
pip install collectbase
cd 你的仓库              # 已经有东西也没关系,这才是默认场景
cb init --layers facts,notes,beliefs
```

---

## 为什么

智能体的记忆会腐烂,腐烂的方式很具体:**它把自己的推论当成观测,下一轮基于那个推论再推**。三五轮之后内容还是自洽的、引用得头头是道,只是跟现实脱钩了。

不是模型不够聪明,是它的工作记忆里**观测和推论长得一模一样**——都是文件,都能改,改完看不出区别。

分层就是在文件系统这一层把这条回路切断。这不是访问控制,是**认知卫生**:让"我看到的"和"我以为的"在文件系统上是两种东西。

---

## 用起来

```bash
git checkout stack                  # 看得见所有层,声明哪层都行

# 事实进来(人 / 采集脚本 / 外部同步)
cp ~/.claude/projects/…/session.jsonl project/log/2026-08-14.jsonl
git add -A && git commit -m "[facts] 采集 8-14 会话"

# 智能体上班
$EDITOR project/why-it-broke.md
git add -A && git commit -m "[beliefs] 这次故障的根因判断"

# 它试图动事实
$EDITOR project/api.md                    # → EACCES,当场失败
git commit -am "[beliefs] 顺手修一下"      # → 拒绝:该路径属于层 [facts]
```

**站在哪条分支上结果都一样**:改动落到 `[层名]` 声明的那条权威分支上,再 merge 进 `stack`。站在 `layer/<L>` 上时,你那个提交**就是**权威对象,SHA 原样。

提交信息必须以 `[层名]` 开头,声明这次改动属于哪一层。于是 `git log` 本身成了一份认知审计:

```
$ git log --oneline --first-parent stack
94c7c4a [beliefs] 推翻上一版根因
0b1f831 [facts]   采集 8-15 日志
1fc8481 [beliefs] 这次故障的根因判断
5de0292 [notes]   api 的调用约束笔记
```

**只看某一层**,或者只看某一层的演化——它们是权威分支,不是投影:

```bash
git checkout layer/notes            # 只有 notes 自己的文件
git log --oneline layer/beliefs     # 只有智能体自己干过的事

```

---

## 它做的四件事

**分层**。权威是 `layer/*`,每条只放自己那一层的文件、线性、必须 FF。`stack` 是它们 merge 出来的视图,每个 merge 节点挂着那次权威提交本身(同一个 SHA),信息逐字相同。

**守卫**。`reference-transaction` 检查落进 ref 的**每个提交**,而不是"谁发起的这次更新"。所以 `--no-verify`、`merge`、`reset --hard`、`rebase`、`cherry-pick` 一个都绕不过去;而一个内容合规的 `cherry-pick` 自然放行,不需要为它开特例。

**二进制外置**。二进制不进 git,进 `blob/<年>/<月>/<日>/<sha256>.<ext>`,原地留一条相对软链。仓库因此保持在几百 KB 量级,而 `blob/` 本身可以当媒体库浏览。

**校验**。`cb check` 验四条不变量。其中 I4(重新哈希比对)是唯一能发现"作为事实的截图被悄悄换掉"的手段——软链受分层保护,它指向的字节不受。

---

## 全部命令

```
cb init --layers a,b,c      在已有仓库上建立拓扑:定起始点位、既有内容归最底层、装 hook
cb check                    校验 I1–I4
cb rebuild                  从各层 tip 重建 stack(它是构建产物)
cb blob gc [-n]             删掉没有任何提交引用的 blob
cb blob push <url>          把 blob 传到异地(file:/// 或 s3://)
cb blob pull <url>          把缺的 blob 拉回来,写盘前验 sha256
cb serve                    路径 CRUD 的 HTTP 面 + 分层文件管理器
```

**其余一切用 git。** 读的 git 已经有了(`git status` / `git log` / `git branch --list 'layer/*'`;甚至"这个路径归哪层"都不用问——事实层是 444,`ls -l` 就是答案),写的由 hook 自动发生。

---

## 起始点位:它不接管你的过去

`cb init` 面向的是**已经有东西的仓库**。它在当前 HEAD 上放一个 `layers` 锚定文件,那个提交就是**起始点位**:

- 既有内容全部划归最底层——已经存在的东西就是"给定的"。
- `layer/<底层>` 直接指向起始点位,于是**既有的全部历史原样成为事实层的历史**,零迁移。
- 起始点位**之前**的历史不受任何约束:没有层标签的旧提交、混在一起的二进制、任意的分支结构,全部原样保留。

collectbase 从某个提交开始接管未来,不改写过去。

---

## 边界

- **不管内容格式,也不管文件之间的关系。** 谁引用谁、哪条结论取代哪条,是使用者的事。
- **不做采集**,不推给任何下游。
- **本地 hook 是认知卫生,不是安全边界。** 直接手写 `.git/refs/` 或拆掉 `core.hooksPath` 仍能绕过——那是拆机制不是绕机制。要防对抗,用 uid 边界(事实文件归另一个 uid、模式 444,智能体 `chmod` 是内核拒绝)。

---

## 设计文档

`docs/v2/` 下有一篇契约 + 四篇子设计,以及七个可跑的实验脚本——文中每一处"实测"都能自己重现。

- [`docs/v2/DESIGN.md`](docs/v2/DESIGN.md) — 立意与契约
- [`works/branch-topology.md`](docs/v2/works/branch-topology.md) — 权威在 `layer/*`、`stack` 由 merge 得到、`[层名]` 声明(附三个被否方案及其实测)
- [`works/blob-store.md`](docs/v2/works/blob-store.md) — 二进制外置 + 软链
- [`works/cli.md`](docs/v2/works/cli.md) — 锚定、init、hook、别的分支为什么捅不进来
- [`works/server.md`](docs/v2/works/server.md) — `cb serve`(计划中):路径 CRUD + blob 传对象存储

v1(采集器形态)已停止演进,存档在 [`docs/v1/`](docs/v1/)。

---

Apache-2.0
