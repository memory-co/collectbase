# collectbase docs

collectbase 的分篇设计。顶层立意 + 契约在仓库根的 [`../DESIGN.md`](../DESIGN.md);本目录展开各子系统。

## 阅读顺序

1. [`../DESIGN.md`](../DESIGN.md) — 立意、三角色(engine / worker / sink)、剥离面、里程碑。**先读它。**
2. [`worker.md`](worker.md) — ★ **worker 机制与编写指南**。怎么用几十行写一个 worker:声明监听哪些文件、把一条记录解析成标准 round,其余(watch / 冷扫 / 哈希 / 游标 / 冲突重试 / checkpoint)全归 engine。
3. [`session-format.md`](session-format.md) — **标准化产物**。worker 归一的落点:`Round` / `ContentBlock` 的字段、构造器、幂等键、role/speaker、session 级元数据。

## 一句话地图

```
你写:  worker —— 声明 glob + 把一条记录变成 Round(session-format.md)
                        │
engine 替你做: watch / backfill / 哈希跳过 / 游标 seek / 乐观并发 + 冲突重试 / checkpoint
                        │
                       sink —— ensure + append 推进 memory(DESIGN.md §6)
```
