# collectbase docs

按版本分目录。每个版本是一套自洽的设计,不改写历史版本——新一版另起目录。

- [`v2/`](v2/) — **当前方向**:分层记录文件系统,层即 Git 分支。见 [`v2/DESIGN.md`](v2/DESIGN.md)。
- [`v1/`](v1/) — 已停止演进。摄入边界:engine / worker / sink 三角色,采集会话推给 memory system。入口 [`v1/README.md`](v1/README.md),立意文 [`../DESIGN.md`](../DESIGN.md)。

v2 是一次定位转向,不是 v1 的增量:v1 的 worker / sink 整体作废,collectbase 从"采集服务"变成"分层文件管理 CLI"。
