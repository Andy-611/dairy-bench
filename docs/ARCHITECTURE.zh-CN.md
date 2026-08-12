# V6 架构与不变量

## 1. 模拟边界

当前场景 `flow.dairy.base.s9.v6` 包含 9 家独立公司和 52 个经营周。权威时钟是
`SimDay`，一个 Episode 共 364 天，不再存在分钟级经济时间。

```text
TradingCalendar
└── Week 1..52
    ├── Monday..Saturday：决策日
    └── Sunday：结算日
```

核心强类型边界为：

```text
AgentTurn -> CompanyDecision -> DecisionEnvelope -> EconomyEngine -> DecisionOutcome
```

身份、模拟日期、状态版本和应用顺序由 Runtime 提供，Agent 无权填写。

## 2. 模块职责

- `domain/calendar.py`：日期、星期和 Episode 边界。
- `domain/models.py`：场景、观察、事件、周快照和评分契约。
- `runtime/models.py`：决策、Wake、Scheduler 事件和 Journal。
- `economy/engine.py`：唯一经济状态修改者。
- `runtime/episode.py`：推进日期、调用策略、提交结果。
- `runs/`：独立 RunJob 生命周期与并行。
- `storage/store.py`：Journal/Checkpoint 原子持久化。
- `timeline/`：把持久化状态投影成 52 周、每周 7 天的 UI 数据。
- `agents/providers/`：唯一模型边界，所有调用经 NewAPI。

依赖方向始终指向强类型领域契约；存储、Web 和 UI 都不能越权修改经济状态。

## 3. 每周生命周期

### 周一

1. 开启经营周。
2. 用原有 Seed 公式实现每家生产企业的私有产能与基础单位成本。
3. 重置本周订单簿、零售价、已用产能和 Turn 预算。
4. 唤醒全部 9 家公司。

### 周二至周六

每天固定执行：开始日期 → 完成到期生产/加工 → 完成到期运输 → 合并同公司 Wake →
同日并行推理 → 按确定性顺序串行应用决策。

订单和零售价持续到周六。生产、加工与运输均耗时 1 天，并且不能跨周。

### 周日

周日没有 Agent 调用，顺序固定为：

1. 完成到期生产与运输；
2. 关闭两个批发现货订单簿并释放未成交抵押；
3. 每家零售商一次性结算消费者需求；
4. 删除本周到期库存；
5. 保存唯一 `WeekSnapshot`；
6. 进入下一周。

该顺序允许周六承诺在消费结算前到达，同时禁止结算后的临时决策。

## 4. 公式频率

本次迁移改变的是时间频率，不是经济公式：

- 牧场 `normal_capacity=60`、加工厂 `normal_capacity=50` 是
  `CapacityFunction` 参数，不是固定实现产能；
- 周产能仍保留 Seed、持续性、波动和上下界；
- `base_demand=40` 是 `DemandSpec` 参数，不是固定销量；
- 潜在需求仍保留 Seed 冲击和连续价格—需求曲线；
- 成本仍使用原有凸累积成本函数。

产能/成本每公司每周实现一次，潜在需求每零售商每周实现一次并在周日结算。初始现金、
参考价值与公式参数不乘以 7。

## 5. 库存、市场与物理承诺

- 原奶、盒装奶分别在第 2、4 个周结算到期；
- FEFO 抵押和运输保留原 Lot 到期周；
- 买单用现金全额抵押，卖单用真实库存全额抵押；
- 目标 Quote Ladder 原子地 keep/place/replace/cancel 最多三层；
- 仍按价格—时间优先、Maker 挂单价成交；
- 成交时转移现金，1 天运输后库存才可用；
- 每家公司最多一个活跃生产资源；
- 所有经济数量使用 `0.0001` 精度。

## 6. Attention 与 Turn 上限

每个决策都包含 `AttentionPlan`：`review_after_days` 可为空或为 1–2 天；省略时在本周
仍有决策日的情况下默认 1 天；最多 3 个报价 Alert；Review 不能进入周日或下一周。

同一公司同一天的事件、Alert 和 Review 会合并。每家公司每天最多一个 Agent Turn、
每周最多六个；达到上限会写入显式审计事件并抑制后续 Wake。周日不消耗 Turn。

## 7. 确定性并发

同一天被唤醒的公司看到同一个应用前状态，模型可以并行推理，但经济应用顺序固定：

```text
sort_key = SHA256(seed | absolute_day | company_id)
```

每个 Turn 持久化 `apply_sequence`，所以网络延迟、返回先后和其他并行 Run 的负载不会
改变经济结果。不同 RunJob 各自拥有 Runtime、Scheduler、经济状态、Checkpoint、
Journal 与策略实例；每套凭据 Profile 拥有一个 NewAPI HTTP 连接池，所有 Profile 共享
同一个有界 Semaphore。

## 8. 持久化与 Replay

一次原子 Progress 事务同时保存新增 `TurnRecord`、`SystemStepRecord`、完整 Checkpoint
和已完成周进度。完成事务保存最终 `EpisodeResult` 并删除 Checkpoint。停止/中断 Run
使用相同 Run ID 恢复。

Completed Replay 使用源 Observation/Decision，但重新计算 Engine Outcome；Journal、事件、
快照、分数或质量任一漂移都会失败。V6 使用新的 `runs-v6.sqlite3`，不读取旧 Runtime
Payload。

## 9. 评分

52 个周快照完整后计算：

```text
E_raw = Σ_i [V_i(T) - V_i(0)]
E_ref = 52 个 retailer-week 市场的 Seed 最大净价值
E = clip(E_raw / E_ref, 0, 1)
F = 1 - mean(各层 Gini) / (2/3)
B = 破产公司数 / 9
Score = 100 × E × sqrt(F × (1 - B))
```

`V_i` 为现金加参考价库存。`s9-enterprise-v4` 在任一周快照中发现公司净资产小于等于
`1.0000` 时，即将该公司计为破产，且全程只计一次。协议质量独立计算，任意无效
Agent Turn 都会使结果只能用于诊断。

## 10. 必须保持的不变量

1. Episode 恰好完成 52 次周结算和 52 个周快照。
2. 每个 Timeline Week 恰好包含按序排列的 7 个 Day Frame。
3. 公司只在周一至周六决策，且每公司每天最多一次。
4. 周日顺序固定为完成承诺、关市、消费、过期、快照。
5. 周产能和需求必须来自公式实现，不能写死输出。
6. Job、运输、Review 和挂单都不能跨周。
7. 同日并行推理必须按确定性顺序串行应用。
8. 只有 EconomyEngine 能修改经济状态。
9. Journal/Checkpoint 原子提交，Replay 漂移必须失败。
