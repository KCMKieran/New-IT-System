---
id: OPT-0053
title: verify.sh 硬闸里有个不稳定测试：test_run_scan_fast_burst_preserves_slow_tier_alerts（实测 clean HEAD 1/8 轮失败）
status: ready
priority: P1
area: backend
effort: S
created: 2026-07-17
related: [[OPT-0041]] [[OPT-0051]] [[OPT-0012]] [[OPT-0037]]
---

## 背景

`verify.sh` 是项目 canonical 的红/绿验收闸门（见 [[project_verify_gate]]）。
2026-07-17 做 login-ip geo 改造（`IP → IP (CC)`）时，verify.sh 报
`VERIFY: FAIL — backend pytest`，唯一失败是：

```
FAILED tests/test_scheduler_tiers.py::test_run_scan_fast_burst_preserves_slow_tier_alerts
```

该测试与当时的改动**毫无关系**（scheduler tier vs login-ip CRM push，零共同 import）。
排查后确认：**它是不稳定测试（flaky），非本次改动引入，也非测试顺序依赖。**

## 证据（2026-07-17 实测，勿重新调研）

用一个干净的 `git worktree`（detached HEAD = 076591c，**不含任何未提交改动**）做对照：

| 实验 | 结果 |
|---|---|
| 单独跑该测试 | ✅ 通过 |
| 单独跑整个 `test_scheduler_tiers.py` | ✅ 通过（但见下：偶发） |
| **同一条命令连跑 8 轮（clean HEAD）** | **❌ 1/8 轮失败** ← 决定性证据：与改动无关 |
| 同一条命令连跑 8 轮（含 geo 改动的工作树） | ❌ 3/8 轮失败（同一枚硬币，样本小） |
| clean HEAD + `.env` 跑全量 | ✅ 599 passed / 673.84s（该轮没抽中） |
| 含改动的工作树跑全量 | ❌ 1 failed / 627 passed / 678.48s |

⚠ **两个排查陷阱**（都踩过，写下来省下次的时间）：

1. **新建的 worktree 没有 `backend/.env`**（它被 gitignore），导致 4 个 PG/MySQL 测试直接 skip、
   全量只跑 22 秒——看起来像「我的改动让测试慢了 30 倍」。**必须先把 `.env` 拷进 worktree**
   才是同口径对照。补上后 clean 也是 673s，与含改动的 678s 一致（慢是 [[OPT-0051]] 的已知问题，
   不是本次引入）。
2. `pytest -k` 选的是**测试名**不是文件名，所以 `-k 'scheduler'` 会跨文件抓测试；
   用 `--ignore` 排除文件会同时改变选中集合，两边数量对不上就不是同口径。

**快速复现**（比跑 11 分钟全量快得多，~2 秒/轮）：

```bash
cd backend
for i in $(seq 1 8); do
  .venv/bin/pytest tests/ -q -k 'login_ip or last_close or scheduler or housekeep' 2>&1 | tail -1
done
```

## 疑似 root cause（未证实，留给实施者验证）

`app/core/burst_open_scheduler.py`：

- `:1148` `threading.Thread(target=_locked_scan, daemon=True).start()` —— 扫描跑在 **daemon 线程**里
- `:51` `_scan_lock = threading.Lock()`；另有 `_gap_trade_lock` `:76`、`_rebate_arb_lock` `:84`
- 测试断言的是**模块级共享状态** `bs._latest_result`（`tests/test_scheduler_tiers.py:400/409`）

推断：上一个测试留下的 daemon 线程仍在改写 `_latest_result`，而本测试已经在断言它 →
读到别的测试写的 alert 集合 → `rule_ids` 断言挂。daemon 线程不随测试结束回收，
pytest 也不会等它们，所以表现为「偶发」。

**实施者请先证伪/证实这一点再动手**——不要直接加 `sleep` 或 retry 掩盖症状。

## AC

1. 用上面的「快速复现」命令连跑 **≥30 轮，0 失败**（当前 clean HEAD ≈1/8 失败）。
2. 定位并写明真正的 root cause（是否 daemon 线程 + 共享 `_latest_result`），
   修法要治因不治标：**不接受** `time.sleep()`、`pytest-rerunfailures`、
   `@pytest.mark.flaky` 这类掩盖手段。
3. 如果确认是共享模块状态，给 `_latest_result`（及同类模块级状态）加 fixture 级隔离，
   或让 `_run_scan` 在测试里同步执行（不起线程）。
4. `./verify.sh` 连续 2 轮全绿。
5. 如果 root cause 是「生产代码的线程与共享状态本身有竞态」（而非纯测试问题），
   **单独 file 一个 OPT**——那是 prod bug，不是测试 bug，别混在一起修。

## 为什么是 P1

它坐在**硬闸**里。一个 ~12% 概率随机变红的闸门，比没有闸门更糟：
它训练所有人（和所有 agent）把红色当噪音重跑一次，
于是**真实**的回归也会被当成"又抽中那个 flaky"而放过去。
[[OPT-0051]] 让这道闸慢（11 分钟），本单让它不可信——两个叠加，闸门实际已经失效。

## 开放问题（待用户/实施者拍板）

- 与 [[OPT-0051]] 合并做还是分开？0051 是「测试直连云 DB → 慢 + 挂死」，本单是「共享状态竞态 → 不稳定」，
  root cause 不同，但都命中同一道闸门、且都要动测试基建。建议**分开修、0051 优先**（慢是每轮都痛，
  flaky 是偶发），但如果实施者发现 0051 的 mock 化顺手就把线程隔离了，合并也合理。
- `test_run_scan_slow_preserves_fast_tier_alerts`（`tests/test_scheduler_tiers.py:418`，对称的那个）
  是否有同样问题？本次没测到它失败，但它读同一份共享状态，**很可能是同一个 bug 的另一面**。
