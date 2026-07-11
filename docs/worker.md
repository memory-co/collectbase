# worker — 机制与编写指南

> 一个 worker = 一个数据来源的适配器。本篇讲**怎么写一个 worker**,以及 engine 替你扛掉了哪些活。目标只有一个:**让你用几十行、只写来源特有的那部分,就把一种新来源接进来**——声明监听哪些文件,把一条原始记录变成标准 `Round`,别的不用管。
>
> 标准产物(`Round` / `ContentBlock`)的字段与构造器在 [session-format.md](session-format.md);engine 的 7 步驱动路径在 [../DESIGN.md](../DESIGN.md) §8。

---

## 0. 心智模型:你写「解析」,engine 管「搬运」

写 worker 时,脑子里只装两件事:

1. **哪些东西是 session** —— 一个 glob(`~/.claude/projects/**/*.jsonl`),或一个远端列表。
2. **一条原始记录怎么变成一个标准 round** —— `to_round(record) -> Round`。

剩下的全是 engine 的事。下面这张表是本篇的核心论点——**绝大多数代码不该由 worker 作者写**:

| 关注点 | 谁做 |
|---|---|
| 发现新文件 / 文件被改(watch) | **engine** |
| 启动冷扫 backfill(枚举现存所有 session) | **engine** |
| 整源内容哈希 → 没变就整源跳过 | **engine** |
| 定位游标、seek 到上次读到的位置、校验 offset hint | **engine** |
| 只把「游标之后的新记录」喂给你 | **engine** |
| 乐观并发 append、冲突了重读重推一次 | **engine** |
| checkpoint(sha / last_round_id / offset)读写 | **engine** |
| session_id 铸造(`sess-<loc8>-<lastseg>`,跨 endpoint 防撞) | **engine** |
| 一条记录坏了跳过、一个源坏了熔断、别的照跑 | **engine** |
| —— | —— |
| 声明**哪些文件**是 session(glob) | **你** |
| 一条记录 → `Round`(`to_round`) | **你** |
| 哪个字段是 **round_id**(`round_id`) | **你** |
| session 级 metadata(project / cwd / …,可选) | **你** |

参照物:memory.talk 现在的 `claude_code` adapter 310 行、`codex` adapter 339 行——**真正来源特有的只有 ~40 行**(分类 + 内容块映射),其余是哈希 / rglob / 行 seek / offset 校验 / 游标这类每个来源都一样的样板。collectbase 把样板收进 engine 的 base class,你只写那 40 行(§4 的例子)。

## 1. 选哪一档

worker 分三档,按来源形态挑最省的那档:

| 基类 | 适用来源 | 你要实现 |
|---|---|---|
| **`JsonlWorker`** | append-only 行日志(每行一条 JSON:claude-code / codex / 多数 agent 工具) | `to_round` + `round_id` |
| **`FileWorker`** | 会被整体重写的文件(整份 JSON / SQLite 导出 / 一条消息一个文件的目录) | `parse(path) -> Iterable[Round]` |
| **`Worker`** | 非文件 / 异形(HTTP API、webhook、数据库) | 裸端口 4 方法(§7) |

> **90% 的 agent 来源是 `JsonlWorker`。** 先默认它;文件不是「行追加」再退到 `FileWorker`;根本不在磁盘上再下探到裸 `Worker`。

`JsonlWorker` 与 `FileWorker` 的差别只在**性能与增量策略**,产物完全一样:

- `JsonlWorker` **增量读**:engine 用缓存的行 offset seek 到上次位置,只解析新行——大文件(几万行的长会话)也 O(新增行)。
- `FileWorker` **全量解析**:每次变更把整个文件 `parse` 成 rounds,engine 再按 round_id 切出「游标之后」的尾巴——写起来最简单,适合中小文件或非追加格式。

## 2. 监听:声明式,不写监听代码

你**不**调用 watchdog、**不**写轮询循环。文件型 worker 只声明「哪些文件是我的」,engine 从中推导出 watch 根目录并装监听:

```python
class ClaudeCodeWorker(JsonlWorker):
    source = "claude-code"                      # 来源名(全局唯一)
    default_location = "~/.claude/projects"     # 用户没配 location 时的默认根
    glob = "**/*.jsonl"                         # 根目录下哪些文件是 session
    ignore = ["**/tmp/**"]                      # 可选:排除
```

- **`glob` 决定一切**:engine 用它跑 backfill 冷扫(`root` 下匹配到的每个文件 = 一个 session)、并从 glob 的非通配前缀推出要 watch 的目录;
- **一文件一 session**(默认):`session_id` 取文件名 stem;下面 §3 讲怎么改;
- **live 与 backfill 同一条路**:文件被 touch → engine 对那一个文件跑同步;启动冷扫 → engine 对 glob 匹配到的每个文件跑同一段。你感知不到区别。

**非文件来源**声明监听节奏而不是 glob:

```python
class OpenclawWorker(PollWorker):
    source = "openclaw"
    poll = "30s"           # engine 每 30s 调一次你的 list_remote()
```

## 3. session:一个文件怎么对应一个 session

默认 `session_id = path.stem`(文件名去后缀)。两种常见变体,各一个 hook:

```python
class CodexWorker(JsonlWorker):
    glob = "**/rollout-*.jsonl"

    # 变体 A:id 藏在文件内的首条 meta 记录里,文件名只是兜底
    def session_id(self, path, head):
        # head = 首条已解析记录(可能为 None:空文件 / 首行不是 meta)
        return (head or {}).get("id") or _uuid_from_filename(path)

    # 变体 B:给这个 session 挂点来源元数据(可选)
    def describe_session(self, path, head):
        return {"project": unquote(path.parent.name), "path": str(path)}
```

- `session_id(path, head)` —— 返回**上游原始 id**;engine 再用 `mint_session_id` 铸造成 canonical `sess-<loc8>-<lastseg>`(你不用碰,见 [session-format.md](session-format.md#session_id));
- `describe_session(path, head) -> dict` —— 任意 session 级元数据;`created_at` / `cwd` 默认从**首条 round** 自动取,要改写才覆盖这个 hook;
- **一文件多 session / 多文件一 session** 属于少数派:override `sessions_in(path) -> Iterable[str]`,或直接下探裸 `Worker`(§7)。

## 4. 完整例子:claude-code 从 310 行到 ~40 行

`JsonlWorker` 作者只需写两个方法。engine 负责按行 seek、只喂新记录、哈希跳过、游标、冲突重试:

```python
from collectbase import JsonlWorker, Round, Text, Thinking, ToolUse, ToolResult
from urllib.parse import unquote

class ClaudeCodeWorker(JsonlWorker):
    source = "claude-code"
    default_location = "~/.claude/projects"
    glob = "**/*.jsonl"

    # 哪个字段是 round_id(幂等键;engine 用它对齐已存 round)
    def round_id(self, rec):
        return rec.get("uuid")

    # 一条记录 → 一个标准 round;返回 None = 跳过这条(摘要 / meta 行)
    def to_round(self, rec):
        t = rec.get("type")
        if t not in ("user", "assistant"):
            return None
        role, speaker = _classify(rec)                 # ← 来源特有:user 行 4 桶分类
        content = [b for b in map(_block, _content_of(rec)) if b]   # ← 来源特有:块映射
        if not content:
            return None
        return Round(
            id=rec["uuid"], role=role, speaker=speaker,
            at=rec.get("timestamp"), parent=rec.get("parentUuid"),
            cwd=rec.get("cwd"), sidechain=bool(rec.get("isSidechain")),
            content=content,
        )

    def describe_session(self, path, head):
        return {"project": unquote(path.parent.name), "path": str(path)}
```

`_classify` / `_block` / `_content_of` 是作者自己的纯函数——**这才是来源真正独特的部分**(claude-code 把 human / tool_result / harness 注入 / 真人输入四样塞进 `type:"user"`,得拆开;各类 content block 映射到标准块)。相比现状,消失的是:`watch_roots` / `list_sources` / `probe` / 文件读取 / `sha256` / `_locate_start` / `_line_round_id` / `next_line_offset` / rglob——**全归 engine**。

`round_id` 也可以**合成**(codex 的一些记录没有稳定 id):

```python
def round_id(self, rec):
    return rec.get("id") or "r-" + hashlib.sha1(canonical(rec)).hexdigest()[:16]
```

## 5. `FileWorker`:整文件解析

文件不是行追加(整份 JSON 被重写、SQLite 导出、一条消息一个文件),用 `FileWorker`,写一个 `parse`:

```python
from collectbase import FileWorker, Round, Text

class NotesWorker(FileWorker):
    source = "notes"
    glob = "**/*.session.json"

    def parse(self, path):                 # 返回按顺序的全部 round
        doc = json.loads(path.read_text())
        for m in doc["messages"]:
            yield Round(id=m["id"], role=m["role"],
                        at=m.get("ts"), content=[Text(m["body"])])
```

- engine 先对整源哈希:**没变就跳过,连 `parse` 都不调**;
- 变了就 `parse` 出全部 round,engine 找到游标 `after_round_id` 的位置、把**之后的**尾巴推给 sink——你不接触 offset;
- 代价:每次变更重读整文件。中小文件无所谓;大到几万条再考虑改写成 `JsonlWorker`。

## 6. 归一产物:吐标准 `Round`

`to_round` / `parse` 的返回值是 `Round`。构造器对作者友好,别去手搓底层 pydantic:

```python
Round(
    id="…",                 # 必填:上游 round_id(幂等键)
    role="assistant",       # human / assistant / tool / system
    content=[               # str 会自动包成一个 Text 块
        Text("好的,我来改"),
        Thinking("先看 sync.py 的游标逻辑…"),
        ToolUse("Read", {"file": "sync.py"}),
        ToolResult("1\tclass SyncWatcher…"),
    ],
    at="2026-07-11T…",      # 可选:时间戳
    speaker=None, parent=None, cwd=None, sidechain=False, usage=None,
)
```

内容块家族(`Text` / `Thinking` / `Code` / `ToolUse` / `ToolResult` / 通用 `Block(type=…, **fields)`)、字段语义、`index` 由 server 分配的幂等规则、role/speaker 取值——全在 [session-format.md](session-format.md)。**worker 只做格式归一,不做语义加工**(不总结、不结晶):内容如实进,如实出。

## 7. 裸端口 `Worker`:什么时候下探

来源根本不在磁盘上(HTTP API、webhook、DB),或映射太特殊,继承裸 `Worker`,实现 4 方法(即 [DESIGN.md](../DESIGN.md) §4 的契约)。`PollWorker` 是它的一个便捷子类(engine 按 `poll` 间隔驱动):

```python
class OpenclawWorker(PollWorker):
    source = "openclaw"
    poll = "30s"

    def list_remote(self):                 # 相当于 list_sources:枚举远端 session + 其 ETag
        for s in http_get(f"{self.location}/sessions", auth=self.auth_key):
            yield Probe(source_id=s["url"], session_id=s["id"], sha256=s["etag"],
                        created_at=s["created_at"])

    def fetch(self, source_id, after_round_id):   # 相当于 read_after:拉游标之后的 rounds
        page = http_get(source_id, params={"after": after_round_id}, auth=self.auth_key)
        return [Round(id=m["id"], role=m["role"], content=[Text(m["text"])])
                for m in page["messages"]]
```

engine 对它照跑同一条 7 步路径:`sha256`(这里是 ETag)短路、`ensure` 问游标、`fetch` 增量、`append` + 冲突重试、checkpoint。per-source `try/except` 让一个坏 endpoint 不牵连别的。

## 8. 生命周期与错误隔离(你不用写,但要知道)

engine 提供的保证,写 worker 时可以依赖:

- **一条记录抛了**:engine 记一次 parse error、跳过这条、继续下一条(你也可以在 `to_round` 里主动 `return None` 软跳过);
- **一个源抛了**(文件损坏、目录消失、网络挂):per-source `try/except` 只熄这个源的灯,同 worker 的别的源、别的 worker 照跑;
- **debounce**:同一文件的连击(编辑器保存抖动)被合并成一次同步(默认 200ms);
- **顺序保证**:`to_round` 拿到的记录严格按上游顺序,且严格是**游标之后**的新记录——你不需要自己去重或判断「这条推过没」。

## 9. 测试一个 worker

worker 的核心(`to_round` / `parse`)是**纯函数**:喂原始记录,断言 `Round`。不需要起 engine、不碰磁盘、不连 memory:

```python
def test_tool_result_row_classified_as_tool():
    r = ClaudeCodeWorker(location="/x").to_round({
        "type": "user", "uuid": "u1",
        "toolUseResult": {...}, "message": {"content": [{"type": "tool_result", ...}]},
    })
    assert r.role == "tool"
```

监听 / 游标 / 冲突这些 engine 的行为由 engine 自己的测试覆盖,worker 作者不用重测。

## 10. 注册与配置

worker 声明式注册,配置里开哪些、给什么 location:

```python
@register
class ClaudeCodeWorker(JsonlWorker): ...

# 配置(声明文件 / CLI):
workers:
  - source: claude-code                       # location 省略 → 用 default_location
  - source: codex
    location: ~/.codex/sessions
  - source: openclaw                           # 无默认,必须显式给
    location: https://…
    auth_key: …
    poll: 30s
```

engine 启动时按配置实例化对应 worker,给每个装监听、跑 backfill、进入 watching。加一个来源 = 加一个 worker 类 + 配置里加一行,engine / sink / checkpoint 一个字不改。
