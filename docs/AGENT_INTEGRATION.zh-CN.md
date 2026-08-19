# Agent 接入契约

[English](AGENT_INTEGRATION.md) | [简体中文](AGENT_INTEGRATION.zh-CN.md)

## 1. Provider seam

模型公司通过同一个 `DecisionGateway` 接入 NewAPI Chat Completions、Responses 或
Anthropic Messages adapter。每家公司拥有彼此隔离的 `LlmCompanyAgent`、Memory 和
Policy Metadata。每个凭证 Profile 拥有自己的 HTTP 连接池，所有 Profile 共享应用级
请求 Semaphore。

Adapter 只接受恰好一个经过授权的 Function Call。自由文本、多次调用、未知工具，以及
由模型填写的身份或时间都属于无效响应。系统允许一次结构化修复；第二次仍然无效时，
该响应会成为经过审计的协议错误。

## 2. Runtime 权威输入

每次调用都会收到强类型 `AgentDecisionInput`，其中包含 Memory、一个 `AgentTurn` 和
派生出的决策约束。Turn 包含：

- Runtime 权威的身份、模拟日期、状态版本、决策阶段和 Turn 预算；
- 强类型 Wake 原因与新近可见事件；
- 本公司角色、商品、私有周经营状态和公开消费者市场规则；
- 现金、应付运营费、Marked Surplus、库存及到期信息、挂单、订单簿、运输和当前任务；
- 私有经济视图和上一次 Outcome；
- 最多 8 份本公司周报和 8 份公共零售市场周报；
- 仅在周日定价阶段提供的私有采购后 `RetailPricingContext`。

定价上下文包括当前现金、可售数量及账面价值、加权单位成本、本周采购数量/支出/VWAP、
按到期周拆分的库存，以及上周零售价。

Agent 永远看不到 Seed、竞争者私有账本、竞争者成本/库存/采购、准确消费者群体规模
或 WTP、购买力/状态偏移、状态倍率、当前隐藏状态和未来切换。

## 3. 决策阶段与工具

周一至周六的经营阶段允许：

- 牧场：`produce`、`set_quote_ladder`、`idle`；
- 加工商：`transform`、`set_quote_ladder`、`idle`；
- 零售商：`set_quote_ladder`、`idle`。

周日批发市场关闭后，每家活跃零售商会收到一次单独的密封定价调用。唯一允许的工具是
`set_retail_price`，价格用于当周日的消费者结算。所有零售商从同一个状态并发推理，
因此任何一家都无法根据竞争者本周尚未公开的价格作出反应。

任何被接受的输出在进入 Engine 前，都会由 Runtime 包装为带有可信身份与时间的
`DecisionEnvelope`。

## 4. 公开消费者规则

系统明确告诉 Agent：

- 共享市场包含三个 WTP 不同但参数未知的消费者群体；
- 消费者优先选择价格最低、可接受且有库存的零售商；
- 最低价缺货后，需求流向下一低价；
- 同价零售商使用确定性等额分配；
- 低迷、正常和旺盛状态各持续 6～10 周；
- 正常状态与极端状态交替，状态可能改变市场规模、消费者构成和 WTP；
- 每个 Run 的整体购买力也可能改变所有群体的 WTP。

Engine 隐藏准确群体数量、WTP、购买力/状态偏移、状态倍率、第一个极端状态、当前
状态和切换时间。售罄数据属于截尾观察：Agent 只知道实际销量，不知道未满足的潜在需求。

## 5. 周报

私有周报只展示被观察公司的权威经营、现金/价值变化、成本、交易、库存和过期事实。
零售商周报额外包含采购 VWAP、可售库存账面成本、零售价、销量、收入、COGS、毛利、
营业利润、周店铺成本、应付款、售罄率、是否售罄和市场份额。

公共零售周报展示每家零售商的公开价格、销量、市场份额、售罄标记和状态，以及市场
总销量和成交量加权均价。只提供已经完成的过去周，两个窗口都最多保留 8 周。

## 6. Attention 与时间

经营决策必须包含 `AttentionPlan`。`review_after_days` 可以是 `null`、`1` 或 `2`，
最终 Review 必须仍位于本周的周一至周六。最多可以设置 3 个当前尚未成立的 OR 价格
Alert，用于观察 Best Bid/Ask。同一家公司在同一天的多个 Wake 会合并；每家公司每天
最多一个经营调用、每周最多六个。

周日定价调用不安排 Attention，也不占用经营 Turn 预算。无效定价响应会保留上周价格
或第一周默认价，让物理 Episode 能够完成，但整个结果会被标记为协议无效，只能用于
诊断。

## 7. 市场与精度语义

- Quote Ladder 声明某一商品和方向完整的 0～3 档目标状态；
- 未变化的档位保留优先级，发生变化的档位会被原子替换；
- 买单抵押现金，卖单抵押 FEFO 库存；
- Crossing 按静置 Maker 价格成交，运输需要 1 天；
- 生产和加工需要 1 天，且不得跨越本周；
- 所有价格、数量、成本和价值统一使用准确的 `0.0001` 精度；
- 每家活跃零售商知道自己的 `weekly_operating_cost`；该成本在周日收入入账后计提，
  未支付余额会减少 Marked Enterprise Value。

生产型公司的私有周实现会提供 `K` 和 `c`：

```text
C(x) = c*x + curvature*c*x^2/(2*K)
增量成本 = C(u+q) - C(u)
```

## 8. 确定性、Memory 与审计

同一天的调用并发执行，再按照
`SHA256(seed | absolute_day | company_id)` 提交。Provider 返回更快不会获得更高
市场优先级。Agent 的私有 Memory 保存完整 Turn/Outcome 周期；达到 Token 预算时，
较旧周期会进行确定性摘要。权威周报属于状态事实，不是模型生成的摘要。

每次真实 Provider 调用都会记录模型/Profile/协议元数据、Token、延迟、尝试次数、Prompt
身份，以及它是否产生了最终提交的 Turn。凭证不会写入 Journal。Replay 不调用
Provider，并且必须精确复现 Observation、Decision、协议回退和 Engine Outcome。

## 9. 最小 Interface

```python
class CompanyAgent(Protocol):
    metadata: PolicyMetadata

    async def act(self, turn: AgentTurn) -> CompanyDecision: ...
```

Provider adapter 只解析 Tool Call；角色授权属于 Agent seam，调度属于 Runtime，经济
校验与状态变更属于 `EconomyEngine`。
