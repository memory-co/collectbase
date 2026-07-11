# collectbase — 摄入边界:一源一 worker(设计)

> **状态:设计中(founding doc)。** 把 memory.talk 里进程内的 sync(`memorytalk/service/sync.py` 的 `SyncWatcher` + `memorytalk/adapters/`)**整个剥离出来**,变成一个**独立的包 / 服务 `collectbase`**,与 [seekbase](https://—) **平级**:v5 的两个基础服务——**seekbase 管「数据怎么存怎么查」,collectbase 管「经验怎么进来」**。内部按**数据来源**拆成一个个 **worker**:每个来源一个 worker,各自**监听**上游、把上游私有格式**整理成标准 session 格式**、再经 **ingest 接口**推进 memory system。
>
> 定名 **collectbase**(collect = 把散落在各处的原始经验**收拢**进来的底座;与 seekbase 的 seek 对仗:一个管进门、一个管落库与查询)。
>
> 本篇是 collectbase 仓库的**立意 + 契约**文;上游立意见 memory.talk 的 `docs/works/v5/sync-server.md`(本设计的来源),数据层见其 `seekbase.md`。

相关(memory.talk 侧,被剥离的现状):
- `memorytalk/service/sync.py` — 要变成 collectbase 的 **engine**
- `memorytalk/adapters/{base,claude_code,codex,openclaw}.py` — 要变成 collectbase 的 **worker**
- `memorytalk/repository/sync_checkpoint.py` — 要变成 collectbase 的 **checkpoint 库**
- `memorytalk/api/sessions.py`(`POST /v3/sessions/ensure` · `/append`)— 已经是剥离面(见 §11)

---

## 0. 一句话

> **collectbase 站在 memory system 的门外,把各来源的原始会话「看到 → 增量读 → 归一成标准 session → 推进门」这条循环收成一个独立服务;它认识「来源、游标、rounds」,不认识 card、不碰数据库。**

一个 collectbase 实例 = **一个 engine + 一组 worker + 一个 sink + 自己的 checkpoint 库**:

```python
cb = await Collectbase.open(
    checkpoint_dir="./collect",                              # 自己的 sync.db
    sink=HttpSink("http://memory-host:8000", api_key="…"),   # 经 ingest 接口推给 memory
    workers=[                                                 # 声明式:一源一 worker
        ClaudeCodeWorker(location="~/.claude"),
        CodexWorker(location="~/.codex/sessions"),
        OpenclawWorker(location="https://…", auth_key="…"),
    ],
)
await cb.start()      # backfill(冷扫)+ watch(live),两条同一路
info = cb.status()    # phase / totals / per-endpoint / recent
await cb.close()
```

---

## 1. 为什么剥出去

现在 sync 是 memory daemon **进程内**的一个 watcher(watchdog 事件 + 冷扫 backfill),三个 adapter(claude-code / codex / openclaw)编译在主进程里。剥离的理由:

- **职责本来就不同**:sync 是「连接外部世界」的 **connector 层**——`sync.py` 的模块注释早说透了:*sync 认识上游文件格式、游标、sha;IngestService 认识 session / card;两个 domain 焊在一个进程里只是历史巧合*。`sync.db` 里存的是「上游我看到哪了」,这是 connector 状态,不是 memory 状态。
- **故障隔离**:上游格式变了、watchdog 抽风、某来源文件损坏——这些**摄入侧故障不该拖垮 memory daemon**;反过来 memory 重启也不该丢监听。
- **来源会越来越多**:v5 之后接新来源(更多 agent 工具、浏览器、聊天、邮件……)应该是**加一个 worker**,而不是改主进程再发版。
- **云端形态的必然**:memory 在云上时,**采集必须留在数据所在的机器上**——collectbase 天生就是「跑在用户机器上的采集端」,经 HTTP 把经验推给远端 memory。现在剥离就是把这条边界一次切对。

## 2. 定位:memory 的一个普通客户端

```
   上游(各机器上的数据来源)                collectbase(独立进程 / 包)             memory system
  ┌─────────────────────────┐      ┌──────────────────────────────┐      ┌──────────────────┐
  │ ~/.claude/…(jsonl)      │◀─监听─│ worker: claude-code           │      │  ingest 接口      │
  │ ~/.codex/…              │◀─监听─│ worker: codex                 │─push─▶│ POST /v3/sessions │
  │ openclaw(HTTP)          │◀─轮询─│ worker: openclaw              │ HTTP │   /ensure /append │
  │ (未来:浏览器/聊天/邮件…) │◀─监听─│ worker: …(一源一个,可插拔)   │      └─────────┬────────┘
  └─────────────────────────┘      │ engine + 自己的 sync.db        │             seekbase
                                   └──────────────────────────────┘        (collectbase 不碰)
```

- **独立进程、独立生命周期**:自己启停、自己的日志与 status,和 memory daemon 互不陪葬;
- **对 memory 只是一个客户端**:经 **sink**(HTTP `POST /v3/sessions/ensure` + `/append`)推数据——这套 cursor-based、乐观并发的契约**现在就存在**(memory.talk `api/sessions.py`,注释里明写「留给 sync watcher 拆出去后用」),collectbase 基本不用发明新东西;
- **checkpoint 归它**:`sync.db` 跟着 collectbase 走;
- **与 seekbase 平级、互不认识**:collectbase → ingest 接口 → memory system → seekbase。collectbase 永远看不见 seekbase。

## 3. 三个角色:engine / worker / sink

剥离后把原 `SyncWatcher` 一分为三,职责边界画清:

| 角色 | 是什么 | 从哪来 | 认识什么 |
|---|---|---|---|
| **worker** | 一个数据来源的适配器 | `adapters/*.py` | 上游私有格式、如何发现新数据、如何增量读、如何归一 |
| **engine** | 驱动所有 worker 的调度核心 | `SyncWatcher` | 游标、checkpoint、乐观并发冲突、backfill/live 调度、status |
| **sink** | 把归一后的 session 推出去的出口 | `api/sessions.py` 的客户端侧 | 只有 `ensure_session` + `append_rounds` 两个动作 |

**engine 是共享的、与来源无关的那部分**;**worker 是每来源各写一份的那部分**;**sink 是与 memory 的唯一接触面**。加来源只写 worker,engine / sink 不动。

## 4. worker 契约:pull + normalize

**worker = adapter**:实现「怎么发现有新数据」「怎么增量读」「怎么归一」,engine 负责把它们串成同步循环。契约原样保留自 `adapters/base.py`(剥离时几乎逐字带走):

```
worker 契约:
  watch_roots()               → list[Path]      监听哪些目录(fs 型来源);HTTP 型返回 []
  list_sources()              → Iterator[Probe] 枚举当前所有上游 session(backfill 冷扫走这里)
  probe(source_id)            → Probe | None    廉价探一个源:sha256 + 上游 id + metadata
  read_after(source_id,       → ReadAfterResult ★ 归一:从游标后增量读,吐标准 rounds
             after_round_id,                       + next_line_offset(下次 seek 的 hint,对 engine 不透明)
             hint_line_offset)
```

- **normalize 是 worker 存在的核心理由**:`read_after` 返回的是 `list[RoundInput]`(§5 的标准形状)——**上游的花样(claude-code 的 jsonl 结构、codex 的会话文件、将来浏览器 / 聊天记录的千奇百怪)死在 worker 这一层**,sink 只认一种标准格式。加来源不污染 memory 侧 schema。
- **listen 策略声明式**:worker 声明自己怎么被唤醒——`fs-watch`(watchdog,`watch_roots()` 非空)/ `poll`(定时轮询,HTTP 型)/ 未来 `webhook`。engine 按声明装监听器;判定「有没有新数据」的逻辑仍只有一处(§8)。
- **worker 之间完全隔离**:各自的探测 / 退避 / 熔断;一个来源坏了(格式变更、目录消失、网络挂)只熄它自己的灯——engine 的 per-source `try/except` 已经保证这点。
- **session_id 归一**:worker 用 `mint_session_id(upstream_id)` 铸造 canonical id `sess-<loc8>-<lastseg>`——`loc8 = sha256("<source>#<location>")[:8]`,让**同一个上游 id 在不同 endpoint(US / EU)不撞车**;`lastseg` 取上游 id 最后一段保持短。这是 memory 侧唯一认的格式,worker 铸好再推。
- **现有三个 adapter 原地变身三个 worker**:claude-code / codex(fs 型,已实现)+ openclaw(HTTP 型,当前 stub)。

## 5. 标准 session 格式(normalize 的目标)

worker 归一的落点、也是 sink 的 wire 格式。**故意 free-form**:平台会吐 text / code / thinking / tool_use / tool_result……如实保留,下游(read 展示、FTS 抽取)自己投影。

```
RoundInput               一轮对话(worker 归一的产物,server 侧再补 index)
  round_id       str       ★ 上游 uuid;server 用它对齐已存 round(幂等键)
  parent_id      str?
  timestamp      str?
  speaker/role   str?
  content        [ContentBlock]
  is_sidechain   bool
  cwd            str?
  usage          dict?

ContentBlock             一个内容块(type 自由,常见字段显式浮出,其余落 extra)
  type           str       text / code / thinking / tool_use / tool_result / …
  text/language/thinking   常见形状显式浮出
  (extra=allow)            其余原样保留
```

- `index` **不是**输入:server 首写时分配、跨 re-ingest 稳定;worker 只给 `round_id`,server 用它对齐。这是幂等的根。
- **只归一格式,不加工语义**:内容如实、不增删——见 §10 边界。

## 6. sink 契约:ensure + append,冲突重试在 engine

sink 是与 memory 的唯一接触面,只有两个动作(cursor-based、乐观并发、append-only):

```
ensure_session(session_id, source, location, location_label)
    → EnsureSessionResponse{ last_round_id, round_count }        只读探游标,绝不建行

append_rounds(session_id, source, location, …, expected_prev_round_id, rounds, created_at, metadata)
    → status="ok"       { new_last_round_id, appended_count, index_status, … }
      status="conflict" { actual_last_round_id }                 期望游标不匹配 → 让 caller 重算重推
```

- **两种实现,同一契约**:`HttpSink`(打 `POST /v3/sessions/ensure` + `/append`,本地 localhost / 云端带 token)与 `InProcessSink`(直连 `IngestService`,给测试 / 同机嵌入用)。engine 只依赖 sink 抽象,不知道底下是 HTTP 还是进程内——**换传输不换契约**(seekbase 同款纪律)。
- **冲突重试是 engine 的活,不是 sink 的**:sink 只如实回 `conflict` + server 实际游标;engine 收到后按实际游标让 worker 重读、重推一次,二次冲突记日志放弃这轮(逻辑原样搬自 `_send_with_conflict_retry`)。
- **index 结果是独立轴**:`append` 的 `status` 只报「jsonl + 结构化写成没成」;向量索引成败走 `index_status`(ok / partial / failed)另计——collectbase 如实透传进 status,不自己重试(那是 memory 侧 backfill 的活)。

## 7. checkpoint:自己的 sync.db

「上游我看到哪了」是 connector 状态,跟着 collectbase 走。schema 原样带自 `sync_session_checkpoint`:

```
sync_session_checkpoint
  PRIMARY KEY (source, location, session_id)   ← 按 (来源, endpoint, 上游原始 id) 定位
  sha256          TEXT   整源内容哈希 → 「自上次同步变了没」的短路判据
  last_round_id   TEXT   已同步到的游标(上游 round_id)
  line_offset     INT    下次 seek 的 hint(jsonl 行号 / 其他来源自定义,对 engine 不透明)
  updated_at      TEXT
```

- **短路优先**:`probe.sha256 == ckpt.sha256` → 整源跳过,连 `ensure` 都不发。这是省掉绝大多数 no-op 的关键。
- **checkpoint 用上游原始 id 作键**(不是 canonical id):同一份 sync.db 里 US / EU 两个 endpoint 各存各的游标不打架。
- **sync.db 原样迁移**:从 memory.talk 剥离时把现有 `sync.db` 文件直接带到 collectbase 的 `checkpoint_dir`,游标不丢、不重扫。

## 8. 核心同步路径:backfill 与 live 同一条路

engine 的核心不变量——**「判定什么是新数据并推给 memory」的逻辑只有一处** `_sync_one_source(worker, source_id)`,7 步(原样搬自 `sync.py`):

```
1. worker.probe(source_id)          → sha256 + 上游 id + metadata
2. checkpoint.sha == probe.sha ?     → 是则跳过(整源没变)
3. sink.ensure_session(canonical id) → server 现在的游标 last_round_id
4. worker.read_after(after=server_last, hint=ckpt.line_offset) → 增量 rounds(已归一)
5. sink.append_rounds(expected_prev=server_last, rounds)       → 乐观并发追加
6. 冲突?按 server 实际游标重读重推一次,二次冲突记日志放弃这轮
7. checkpoint.upsert(new sha, new last_round_id, new line_offset)
```

- **backfill(冷扫)= 对每个 worker 的 `list_sources()` 逐个跑 1–7**;
- **live(watchdog / poll 事件)= 对被触碰的那个源跑同一段 1–7**;
- **同一段代码、同一套日志、同一套 status**——这是现状最值得保留的设计,剥离时不重写。
- engine 启动时**先起监听器 + worker 队列,再跑 backfill**:backfill 期间到达的 live 事件先入队不丢;backfill 完 → `phase: watching`。debounce 同路径连击合并(默认 200ms)。

## 9. 形态:进程模型 / 传输 / 配置 / CLI

- **进程模型**:先 **单进程内多 worker**(asyncio task 群,当前形态平移),engine 一个 event queue + 一个 worker task 排空;留出 per-worker 子进程的拆分口(隔离更硬、开销更大),不首发。
- **传输**:本地走 localhost HTTP(可选 UDS);云端 HTTP + bearer token + 压缩 + **断线缓冲**(worker 本地攒批,memory 不在也不丢,恢复后按 checkpoint 续推)。
- **两种使用形态**(照 seekbase 的双形态):
  - **嵌入**:`Collectbase.open(sink=InProcessSink(ingest), …)`——同机、进程内,给「memory 与采集同机」的当前部署;
  - **服务**:`collectbase serve`——独立进程 + `HttpSink`,给「采集在用户机、memory 在云」的目标形态。调用代码只换 sink 那一行。
- **配置**:声明文件(哪些 worker 开、location / 凭据 / poll 间隔 / debounce)+ CLI:`collectbase status`(phase / totals / per-endpoint / recent,平移 `/v3/sync/status` 的输出)、`collectbase sync <source>`(单源手动触发)、`collectbase serve`。
- **backpressure**:上游洪峰(一次导入几百个 session)对 sink 限流,worker 各自队列 + 退避,别把 memory 打爆。

## 10. 边界(collectbase 不做什么)

- **只做格式加工,不做语义加工**:worker 的加工止于 **normalize**(私有格式 → 标准 session,内容如实、不增删语义);**不结晶、不治理、不总结**——那是 memory system / agent 的活。
- **不碰 seekbase**:永远只走 sink(ingest 接口),不知道底下是 DuckDB / LanceDB 还是别的。
- **单向**:外部 → memory。它不替 executor 拉召回(那是宿主 / 嵌入契约的事)。
- **不认识 card**:它的世界里只有「来源、游标、rounds、session」。
- **业务无关(向 seekbase 看齐)**:collectbase 包不读 memory.talk 的 Config、不 import memory 侧的 service;它只收注入的 `sink` / `workers` / `checkpoint_dir`。worker 认识具体来源格式,engine / sink / checkpoint 完全通用。

## 11. 从 memory.talk 迁移

剥离面**现在就是干净的**——`api/sessions.py` 的注释白纸黑字:*两个路由都是 `IngestService` 方法的直投,留给「sync watcher 最终拆出进程后」用*。步骤:

| 搬什么 | 从 | 到 collectbase |
|---|---|---|
| engine | `service/sync.py` `SyncWatcher` | `collectbase/engine.py`(去掉对 memory Config 的直依赖,改注入) |
| worker | `adapters/{base,claude_code,codex,openclaw}.py` | `collectbase/workers/*.py`(契约不变) |
| checkpoint | `repository/sync_checkpoint.py` + `sync.db` 文件 | `collectbase/checkpoint.py` + `checkpoint_dir/sync.db`(原样带走) |
| 标准格式 | `schemas/session.py` 的 `RoundInput`/`ContentBlock`/`SourceProbe`/`ReadAfterResult` + ensure/append req·resp | collectbase 自带同款(wire 契约,两侧共享定义) |
| sink | `api/sessions.py` 客户端侧(新写)+ `InProcessSink`(包 `IngestService`) | `collectbase/sink.py` |

memory.talk 侧收尾:主进程删掉进程内 watcher;`memory.talk sync` 命令变成对 collectbase 的控制面(或直接用 `collectbase` CLI);`/v3/sync/status` 的去留看是否还要在 memory 侧聚合展示(倾向:status 归 collectbase,memory 侧退役)。

## 12. 里程碑

- **M1 骨架**:engine + checkpoint + `InProcessSink` + claude-code worker,跑通 backfill/live 7 步,与现状行为等价(同一 `sync.db`、同样 status)。
- **M2 全 worker**:codex worker 平移;openclaw worker 从 stub 落实(HTTP poll + per-session ETag 游标)。
- **M3 HttpSink + serve**:`POST /v3/sessions/ensure`·`/append` 客户端 + `collectbase serve` + CLI;memory.talk 主进程删 watcher。
- **M4 云形态**:bearer token + 压缩 + 断线缓冲 + backpressure 限流。

## 13. 待定

- **进程模型**:单进程多 task vs per-worker 子进程——先前者,留拆分口。
- **worker 配置格式**:声明文件 schema(source 类型 + location + 凭据 + 节奏)最终定形。
- **断线缓冲落盘**:worker 本地攒批放内存还是落 checkpoint 库旁的 spool。
- **openclaw 游标策略**:per-session ETag(优选,现 schema 已支持)vs whole-list cursor。
- **status 归属**:collectbase 独占,还是 memory 侧仍聚合一份。

## 与其他文档的关系

- memory.talk `docs/works/v5/sync-server.md` — 本设计的**来源**;collectbase 是它的独立实现,概念一一对应(sync-server → collectbase,worker → worker,ingest 接口 → sink)。
- memory.talk `docs/works/v5/seekbase.md` — **平级基础服务**;经验经 sink 落库后才见 seekbase;collectbase 的 checkpoint 不放 seekbase(connector 状态,跟着服务走)。
- memory.talk `docs/works/v5/agent.md` — agent 的「摄入(Ingest)」消费的就是 collectbase 搬进来的 session;两者以「数据 session 落库」为交接点(agent 不管怎么搬来的)。
