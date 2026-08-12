# Agent 接入契约

## 1. 模型边界

模型公司只通过 NewAPI Chat Completions Transport 接入。每家公司拥有隔离的
`LlmCompanyAgent`、Memory 和 Policy Metadata；不同 Run 只共享 HTTP 连接池与请求
Semaphore。

Adapter 只接受恰好一个授权 Function Call，拒绝自由文本命令、多工具调用、未知工具，
也不允许模型填写身份或时间。禁用并行 Tool Call；第一次无效响应可进行一次结构化修复，
再次失败则记录为协议无效 Turn。

## 2. Runtime 提供的输入

每次调用收到：

```text
AgentDecisionInput
├── memory
├── turn: AgentTurn
└── decision_constraints
```

`AgentTurn` 包含 Runtime 权威的 `turn_id`、`company_id`、`sim_day`、
`state_version`，本周 Turn 序号/上限，强类型 Wake 与因果来源，以及：

- 本公司角色、公开场景规则、商品和需求曲线；
- 本公司的私有周产能/成本实现；
- 可用/抵押现金、Marked Surplus、库存及到期周；
- 本公司挂单与相关匿名订单簿；
- 在途运输、活跃生产任务、私有现金流与单位经济性；
- 新可见事件和上一次 Outcome。

Agent 看不到 Seed、其他公司的私有账本、隐藏需求冲击、隐藏产能/成本实现或未来事件。

`decision_constraints` 明确给出 1 天生产/运输周期、1 天决策间隔、默认/最大 Review 天数、
距离周日的天数以及已用/剩余产能；这些是规则事实，不是建议。

## 3. 输出

Agent 必须调用一个符合角色权限的决策工具，返回：

```text
ActionDecision(action, attention)
IdleDecision(attention)
```

- 牧场：`produce`、卖出 Quote Ladder、`idle`；
- 加工厂：买入/卖出 Quote Ladder、`transform`、`idle`；
- 零售商：买入 Quote Ladder、`set_retail_price`、`idle`。

Runtime 会把验证后的决策与可信身份、日期、状态版本组合成 `DecisionEnvelope`，再交给
`EconomyEngine`。

## 4. AttentionPlan

每个 Action/Idle 都必须包含：

```json
{
  "review_after_days": 1,
  "alerts": []
}
```

- `review_after_days` 只能是 `null`、`1` 或 `2`；
- `null` 在本周还有决策日时使用 1 天默认 Review；
- 实际 Review 必须位于本周 Monday-Saturday；
- 最多 3 个 OR Alert，可观察 `best_bid`/`best_ask`，使用
  `at_least`/`at_most`；
- Alert 在设置时必须尚未成立、对该公司可观察且不能重复。

接受的决策不会自动制造额外下一 Turn。合法 Wake 包括 Review、价格 Alert、本公司成交、
生产/运输完成、拒绝纠正和周开盘。同公司同日 Wake 会合并。

## 5. 周经营规则

- 公司仅在周一至周六决策；
- 每公司每天最多一次、每周最多六次模型调用；
- 周日关闭批发订单簿、一次性消费结算、处理过期，不调用 Agent；
- 生产、加工、运输均为 1 天且不能进入下一周；
- 订单簿和零售价在本周持续，周日后重置；
- 原奶/盒装奶分别在 2/4 个周结算后过期。

生产企业的 `weekly_operation` 给出实际周产能 `K` 和基础单位成本 `c`。已经使用 `u`、
新增数量 `q` 时：

```text
C(x) = c*x + curvature*c*x^2/(2*K)
增量成本 = C(u+q) - C(u)
```

场景中的 Normal Capacity 是公式输入，Agent 必须使用 Observation 中私有的本周实际值。

## 6. 市场语义

- `set_quote_ladder` 是某商品/方向的完整目标状态；
- 0–3 层按最优到最差排序，`[]` 清空该方向；
- 完全相同的价量保留订单身份与优先级，修改层原子替换并失去优先级；
- 买单必须全额现金抵押，卖单必须全额真实 FEFO 库存抵押；
- Crossing 立即按 Maker 挂单价成交；
- 成交时转移现金，购买库存 1 天后到达；
- 价量最多四位小数，生产/加工/Quote 数量至少 `0.0001`。

## 7. 同日确定性应用

同日 Wake 的 Agent 观察同一个基础状态并可并行推理，Runtime 按下式串行应用：

```text
SHA256(seed | absolute_day | company_id)
```

持久化 `apply_sequence` 是唯一经济顺序，模型响应更快不会获得市场优先权。

## 8. Memory、审计与 Replay

应用后，公司私有 Memory 接收完整 Turn/Outcome；压缩过程确定且不会跨公司。每个物理调用
记录模型、Provider、Token、延迟、尝试次数、Prompt 版本/Hash 和是否进入已提交 Turn；
凭证不会写入 Journal。

Replay Agent 不调用模型，必须精确匹配源公司的日期、Observation、Decision 和重新计算的
Outcome。

## 9. 最小实现接口

```python
class CompanyAgent(Protocol):
    metadata: PolicyMetadata

    async def act(self, turn: AgentTurn) -> CompanyDecision: ...
```

Provider Adapter 只负责构造请求、解析唯一 Tool Call 并返回强类型 Union；角色权限、调度和
经济验证仍由 Runtime 与 Engine 统一负责，不能在各 Adapter 重复实现。
