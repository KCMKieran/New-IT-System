---
id: OPT-0019
title: Redis 加 maxmemory + allkeys-lru eviction policy
status: done
priority: P1
area: infra
effort: S
created: 2026-05-19
related: [[OPT-0018]]
---

## 问题

生产 Redis（`new-it-redis-prod`）没配 `maxmemory`，policy 是默认 `noeviction`：

```
maxmemory_human:0B
maxmemory_policy:noeviction
```

当任何 service 写 key 把 Redis 内存写满时，**后续 `SET` 直接返回 OOM 错误**，缓存层静默退化（fallback 到上游数据源），但监控不会自然发现。

当前 `used_memory_peak_human=5.60M` 远没问题，但是**没护栏**。趁现在加一行 compose 就解决，成本最低。

## 背景

OPT-0018 cache audit 里的 🔴 #1 真问题，被识别为 ROI 最高的子项（5 分钟工作量 + 直接消除一个真实失败模式）。

`docker-compose.prod.yml:74-77` 当前 redis-prod 块只有 4 行（image / container_name / restart），没有 `command:` 覆盖默认行为。

## 假设 / 待验证

- [x] 项目所有 Redis key 都有显式 TTL（PnL 30min、IB 10min、Client Return 3h，audit 已确认）→ allkeys-lru 和 volatile-lru 效果等价，但 allkeys-lru 对未来新增的"忘记加 TTL"的 key 更鲁棒
- [x] 256MB 限额是否合理：当前 peak 5.6M（45x 余量），未来加 Client Return 增量 / 新 feature cache 都够用；机器内存够，给 Redis 256MB 没压力
- [ ] 是否同时给 dev compose（`backend/docker-compose.dev.yml` 的 `new-it-redis`）加同样配置：是，避免 dev/prod drift

## 验收标准

- [ ] `docker-compose.prod.yml` 的 redis-prod 服务加 `command: redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru`
- [ ] `backend/docker-compose.dev.yml` 的 redis 服务加同样的 command（dev/prod 一致）
- [ ] **不自动重启 prod redis** —— 改完 compose 后留给用户决定何时 `docker compose up -d redis-prod` 应用
- [ ] 改完文档：在 OPT-0018 audit 的 finding #1 加 link 指向本 OPT 作为已修复
- [ ] 验证方法（用户手动跑或重启后跑）：
  ```
  docker exec new-it-redis-prod redis-cli CONFIG GET maxmemory
  docker exec new-it-redis-prod redis-cli CONFIG GET maxmemory-policy
  ```
  期望：`maxmemory = 268435456`（256 * 1024 * 1024），`maxmemory-policy = allkeys-lru`

## 笔记

**为什么是 `allkeys-lru` 而不是 `volatile-lru` / `allkeys-lfu`**：
- 项目所有 cache 都是"业务热度型"（最近用过的更可能再用），LRU 比 LFU 更直观也够用
- allkeys 而非 volatile：当前所有 key 都有 TTL（等价），但未来某个 service 忘记加 TTL 时，allkeys 仍能正确驱逐；volatile 会让无 TTL 的 key 永远留下来挤占空间
- 不选 `noeviction`（继续 OOM 报错）：缓存路径都有 fallback，宁可丢一些命中也不要写失败的副作用

**为什么 256MB**：
- 当前 peak 5.6M，给 45x 余量
- 真实写满前提（每条 cache entry 估计 50KB，要写满 256MB 需要 ~5400 个未过期 key 同时存在）远远高于当前 + 1 年内增长
- Redis 7-alpine 在 256MB 下内存占用对宿主机毫无压力（机器是 IT 内网服务器）

**不动 RDB / AOF 配置**：这是 OPT-0018 finding #2（显式 volume + 持久化策略）的范畴，不在本 OPT scope。

## 结果

**交付**（commit `61b2666`，merge SHA 由本次 close commit 决定，可用 `git log --grep=OPT-0019 --merges` 找到）：
- `docker-compose.prod.yml:74-81` redis-prod 服务加 `command: redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru`
- `backend/docker-compose.dev.yml:58-65` dev redis 同步同样配置（避免 dev/prod drift）
- 两个文件都加了内联注释指向本 OPT

**与 AC 偏差**：
- AC 里说"改完文档：在 OPT-0018 audit 的 finding #1 加 link 指向本 OPT 作为已修复"—— **未做**，留作 follow-up。
  - 理由：OPT-0018 当前 status=idea，下次有人 claim 它走 audit 时会自然看到 done.md 里 OPT-0019 已 close；强制现在改 OPT-0018 反而污染了那个 idea 的快照

**生效需要用户手动操作**（设计如此，避免 AI 重启生产服务）：
```
docker compose -f docker-compose.prod.yml up -d redis-prod
docker exec new-it-redis-prod redis-cli CONFIG GET maxmemory       # 期望 268435456
docker exec new-it-redis-prod redis-cli CONFIG GET maxmemory-policy # 期望 allkeys-lru
```

**Stage 1 review**：用户选 "No, 直接合并"（4 行 compose 改动，参数选择理由已在背景章节写清，预判 reviewer 不会发现新信号）。

**Follow-up**（不立新 OPT，记一笔）：
- 若 OPT-0018 audit 推进到拆子 OPT 阶段，把"在 finding #1 标记已修"作为顺手活
- 真实 prod Redis 重启时机由运维 / 用户决定，本 OPT 视 compose 改动落盘为完成
