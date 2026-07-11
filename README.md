# collectbase

**摄入边界(ingestion boundary):把各来源的原始会话收拢、归一成标准 session,推进 memory system。**

collectbase 是从 [memory.talk](https://github.com/memory-co) 里进程内的 sync 剥离出来的独立服务,与 **seekbase** 平级:

- **seekbase** 管「数据怎么存怎么查」;
- **collectbase** 管「经验怎么进来」。

内部按数据来源拆成一个个 **worker**(claude-code / codex / openclaw / …):每个来源一个 worker,各自监听上游、把私有格式归一成标准 session、经 ingest 接口推给 memory。它认识「来源、游标、rounds」,不结晶、不治理、不碰数据库。

设计与契约见 **[DESIGN.md](DESIGN.md)**;子系统展开见 **[docs/](docs/)**——重点是 **[docs/worker.md](docs/worker.md)**(怎么用几十行写一个 worker:声明监听哪些文件、把一条记录解析成标准 round)与 **[docs/session-format.md](docs/session-format.md)**(标准化产物)。

> 状态:设计中,实现未开始。
