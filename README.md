# collectbase

**摄入边界(ingestion boundary):把各来源的原始会话收拢、归一成标准 session,推进 memory system。**

collectbase 是从 [memory.talk](https://github.com/memory-co) 里进程内的 sync 剥离出来的独立服务,与 **seekbase** 平级:

- **seekbase** 管「数据怎么存怎么查」;
- **collectbase** 管「经验怎么进来」。

内部按数据来源拆成一个个 **worker**(claude-code / codex / openclaw / …):每个来源一个 worker,各自监听上游、把私有格式归一成标准 session、经 ingest 接口推给 memory。它认识「来源、游标、rounds」,不结晶、不治理、不碰数据库。

collectbase 是**独立模块**:核心只依赖 `pydantic` + `aiosqlite`,对 memory system 只经 HTTP ingest 接口(`/v3/sessions/ensure` · `/append`)说话,不 import 任何 memory 侧代码。

## 独立运行

```bash
pip install -e ".[http,watch]"        # http=HttpSink, watch=fs 实时监听
cp collectbase.example.toml collectbase.toml   # 填 sink.base_url + [[workers]]
collectbase serve  -c collectbase.toml   # 常驻:backfill + watch
collectbase status -c collectbase.toml   # 一次冷扫,打印 totals 退出
```

嵌入用法(库):

```python
from collectbase import Collectbase, HttpSink
from collectbase.workers import ClaudeCodeWorker

cb = await Collectbase.open(
    checkpoint_dir="./collect",
    sink=HttpSink("http://memory-host:8000"),
    workers=[ClaudeCodeWorker()],           # 或 CodexWorker / OpenclawWorker
)
await cb.start(); ...; await cb.close()
```

内置 worker:`claude-code` · `codex`(fs 型)· `openclaw`(HTTP poll 型)。加一个来源见 [docs/worker.md](docs/worker.md)。

## 文档

设计与契约见 **[DESIGN.md](DESIGN.md)**;子系统展开见 **[docs/](docs/)**——重点是 **[docs/worker.md](docs/worker.md)**(怎么用几十行写一个 worker:声明监听哪些文件、把一条记录解析成标准 round)与 **[docs/session-format.md](docs/session-format.md)**(标准化产物)。

> 状态:M1+M2 已实现并测试(29 passed);engine / checkpoint / sink(InProcess + Http)/ 三个内置 worker 齐活,可独立跑通。
