# session-format — 标准化产物

> worker 归一的落点、也是 sink 推给 memory 的 wire 格式。**上游千奇百怪,进门只认这一种形状。** 本篇定义 `Round` / `ContentBlock` 的字段、构造器、幂等规则,以及 session 级的 id 铸造与元数据。
>
> 谁生产它:worker 的 `to_round` / `parse`([worker.md](worker.md))。谁消费它:sink 的 `append_rounds`([../DESIGN.md](../DESIGN.md) §6)。

---

## 0. 为什么标准化

每个来源的原始格式都不一样(claude-code 的 jsonl、codex 的 Responses envelope、将来浏览器 / 聊天记录)。**如果每种花样都渗进 memory 的 schema,加一个来源就要改一次数据层。** collectbase 的分工是:**worker 把上游花样归一成这一种 `Round`,memory 侧永远只见这一种**。加来源不动 schema——新来源自己写自己的 `to_round`,产物形状不变。

目标形状就两层:**session 是一串 round,round 是一串 content block**。

```
session ─┬─ session_id / source / created_at / metadata
         └─ rounds[] ─┬─ round_id / role / speaker / timestamp / …
                      └─ content[] ── ContentBlock(type + 字段)
```

## 1. `Round` —— 一轮对话

worker 用 `Round(...)` 构造器产出;它映射到 wire 上的 `RoundInput`。

| 参数 | 类型 | 含义 |
|---|---|---|
| `id` | `str` **必填** | 上游 round_id(平台 uuid 或合成 id)。**幂等键**,见 §3。 |
| `role` | `str` | 语义角色:`human` / `assistant` / `tool` / `system`,见 §4。 |
| `content` | `list[ContentBlock] \| str` | 内容块;传 `str` 自动包成单个 `Text` 块。 |
| `speaker` | `str?` | 更细的说话人标签(可与 role 不同);默认同 role。 |
| `at` | `str?` | ISO 时间戳(→ wire `timestamp`)。 |
| `parent` | `str?` | 父 round id(→ `parent_id`),表达分叉 / sidechain 树。 |
| `cwd` | `str?` | 该轮的工作目录(agent 来源常用;memory 侧据此判命名空间)。 |
| `sidechain` | `bool` | 是否旁支(→ `is_sidechain`),默认 `False`。 |
| `usage` | `dict?` | token 用量等原始计量,原样透传。 |

**没有 `index`**:序号由 server 首写时分配、跨 re-ingest 稳定——worker 不给、也给不了(它只看到「游标之后的新片段」,不知道全局序)。

```python
Round(id="u1", role="human", content="帮我把 sync 拆出去")

Round(
    id="a1", role="assistant", at="2026-07-11T02:23:00Z", parent="u1",
    content=[
        Thinking("先读 sync.py 看游标逻辑"),
        ToolUse("Read", {"file": "memorytalk/service/sync.py"}),
    ],
)
```

## 2. `ContentBlock` 家族

一个 round 的 `content` 是块数组。**故意 free-form**:平台吐 text / code / thinking / tool_use / tool_result……如实保留,下游(read 展示、FTS 抽取)自己投影。构造器:

| 构造器 | wire 形状 | 用于 |
|---|---|---|
| `Text(text)` | `{type:"text", text}` | 普通文字 |
| `Thinking(text)` | `{type:"thinking", thinking}` | 模型思考 / reasoning |
| `Code(text, language=None)` | `{type:"code", text, language}` | 代码块 |
| `ToolUse(name, input)` | `{type:"tool_use", name, input, text:"[name] input"}` | 工具调用(`input` 是 dict 会序列化;附 `text` 供 FTS) |
| `ToolResult(text)` | `{type:"tool_result", text}` | 工具返回 |
| `Block(type, **fields)` | `{type, **fields}` | **逃生舱**:任何上面没覆盖的块,原样带过(`extra` 允许) |

- **保留类型、不摊平成纯文本**:`tool_use` / `tool_result` 保持 typed,将来 read / search 能按原语义渲染;
- **空块自然消失**:构造器对空 `text` 返回 falsy,`to_round` 里 `[b for b in map(...) if b]` 一句就滤掉;
- **`Block` 是兜底**:遇到新块类型别硬塞进已有构造器,`Block("annotation", ref=…, text=…)` 原样保留,memory 侧至少不丢信息。

## 3. round_id 与幂等

`Round.id` 是整条摄入链路的**幂等根**:

- **对齐,而非追加序号**:server 用 `round_id` 把新片段和已存 round 对齐;同一条重复推送不会产生重复行(`index` 由 server 稳定分配)。
- **游标就是它**:engine 的 `ensure` 拿到 server 的 `last_round_id`,`read_after` / `fetch` 只吐**它之后**的 round;`append` 带 `expected_prev_round_id=last`,不匹配就是 conflict(见 DESIGN §6/§8)。
- **必须稳定**:同一条记录每次解析要得到同一个 id。上游有稳定 id 就直接用;没有就**确定性合成**(对记录规范化后取 hash),别用随机 / 时间——否则每次同步都像新记录,永远追加不完。

```python
def round_id(self, rec):
    return rec.get("id") or "r-" + hashlib.sha1(canonical(rec)).hexdigest()[:16]
```

## 4. role 与 speaker

`role` 是**语义角色**,取四值之一:`human` / `assistant` / `tool` / `system`。`speaker` 可更细(如把不同 harness 注入区分开),默认同 role。

**经验:一个上游「角色」字段常常是多桶的,worker 要拆开。** claude-code 把四样东西都塞进 `type:"user"`:

| 实际是什么 | 判据(稳→脆排序) | 归一到 |
|---|---|---|
| 工具结果回灌 | 有 `toolUseResult` 字段 / 首块 `type:"tool_result"` | `role=tool` |
| harness 注入的 caveat | `isMeta` 标志 | `role=system, speaker=harness` |
| slash 命令产物 | 文本以 `<command-name>` / `<local-command-stdout>` … 起头 | `role=system, speaker=harness` |
| 真人键盘输入 | 以上都不是 | `role=human, speaker=user` |

判据**按稳定性排序**:CLI 级字段(`toolUseResult`)最稳,API 级块类型次之,文本前缀最脆(harness 改文案就会变)——这类分类逻辑是 worker 最该有测试的地方([worker.md](worker.md) §9)。

## 5. session 级字段

一次 append 除了 rounds,还带 session 级信息。worker 通过 `session_id(path, head)` / `describe_session(path, head)` 提供(见 [worker.md](worker.md) §3),engine 组装:

| 字段 | 来源 |
|---|---|
| `source` | worker 的 `source` 类属性 |
| `location` / `location_label` | 该 worker 实例的 endpoint(配置里的 location) |
| `created_at` | 默认取首条 round 的 `at`;`describe_session` 可覆盖 |
| `metadata` | `describe_session` 返回的 dict(project / path / cwd / …) |

### session_id

worker 的 `session_id` 返回**上游原始 id**;engine 用 `mint_session_id` 铸造成 canonical:

```
sess-<loc8>-<lastseg>
  loc8    = sha256("<source>#<location>")[:8]     每个 (来源,endpoint) 一个稳定命名空间
  lastseg = 上游 id 最后一段(最末 '-' 之后)        保持短、仍可人眼辨认
```

- **为什么 engine 铸而不是 worker**:`loc8` 让**同一个上游 id 出现在多个 endpoint(US / EU)也不撞车**——这是跨 endpoint 关注点,worker 不该操心;
- **checkpoint 用原始 id 作键、memory 侧用 canonical id**:engine 在两者间转换(见 DESIGN §7),worker 两个都不碰。

## 6. 边界:只归一格式,不加工语义

worker 的加工**止于 normalize**:私有格式 → 这套 `Round`,内容如实、不增删语义。

- **不总结、不结晶、不治理**——那是 memory system / agent 的活;
- **不判重要性、不打标签、不改写内容**——原样进,原样出;
- 唯一「解释」是**结构映射**(哪个字段是 role、哪个块是 tool_use)和**确定性 id**——这些是格式层的,不是语义层的。

把语义留给下游,是 collectbase 能和 memory 干净解耦的前提:memory 侧永远收到「如实、标准」的 session,怎么理解它由 memory 决定。
