# `EpisodeRuntime` 中文源码阅读版

对应源文件：
[`backend/src/company_bench/runtime.py`](../backend/src/company_bench/runtime.py)

本文件严格翻译 `runtime.py` 中面向读者的英文内容。Python 标识符、类型、
控制流和机器协议字段保持原样；源文件中的运行时错误消息同时给出逐条中文
译文。这样可以阅读中文版，而不会在 Python 包中复制出第二套可执行 Runtime。

## 模块说明

原文位置：`runtime.py:1`

> 隐藏在一个小型 Runtime 接口背后的事件驱动 episode 编排。

## 持久化接口

### `RuntimeStore`

原文位置：`runtime.py:70`

> 用于原子化 Turn/checkpoint 进度的内部持久化接缝。

### `RuntimeStore.save_progress`

原文位置：`runtime.py:73`

> 原子地追加 Journal 条目并替换 checkpoint。

## 执行结果

### `EpisodeExecution`

原文位置：`runtime.py:84`

> 已完成的 episode，以及它的权威逐 Turn Journal。

## `EpisodeRuntime`

### 类说明

原文位置：`runtime.py:105`

> 使用确定性调度和原子命令运行一个 V2 episode。

### `EpisodeRuntime.run`

原文位置：`runtime.py:125`

> 运行、审计并评分一个完整的事件驱动 episode。

### `_merge_wakes`

原文位置：`runtime.py:481`

> 合并同一分钟系统事件执行期间新增的唤醒。

### `_defer_busy_wakes`

原文位置：`runtime.py:506`

> 把外部唤醒移动到公司当前有效命令结束之后。

### `_next_business_time`

原文位置：`runtime.py:534`

> 把冷却边界移动到下一个有效营业时间窗口。

### `_available_after`

原文位置：`runtime.py:549`

> 返回冷却边界；wait 本身不会占用公司。

### `_wake_signals`

原文位置：`runtime.py:715`

> 将 Scheduler 元数据绑定到具有因果关系的 Journal 引用。

### `_replay_origin`

原文位置：`runtime.py:762`

> 持久化原始的即时源 Turn，用作 Replay 来源信息。

### `_validate_replay_source`

原文位置：`runtime.py:1084`

> 把 Replay Agent 绑定到一个匹配的权威源结果。

## 策略描述

### `_policy_descriptors`

原文位置：`runtime.py:1106`

> 按照稳定的场景顺序冻结公司策略身份。

## 运行时错误消息逐条翻译

以下译文与源文件中的错误消息顺序一致。英文字符串保留在可执行源码中，以免
改变 API、测试和持久化协议。

| 源码行 | 英文原文 | 中文译文 |
|---:|---|---|
| 117 | `EpisodeRuntime requires a V2 scenario` | `EpisodeRuntime 需要一个 V2 场景` |
| 119 | `agent_timeout_seconds must be positive` | `agent_timeout_seconds 必须为正数` |
| 151 | `started_at does not match the checkpoint` | `started_at 与 checkpoint 不匹配` |
| 447 | `scheduler ended before the scenario completed` | `Scheduler 在场景完成之前就结束了` |
| 476 | `replay final events, snapshots, or score drifted from the source run` | `Replay 的最终事件、快照或分数与源运行发生了漂移` |
| 489 | `wake event is missing company_id` | `唤醒事件缺少 company_id` |
| 517 | `wake event is missing company_id` | `唤醒事件缺少 company_id` |
| 692 | `wake event is missing company_id` | `唤醒事件缺少 company_id` |
| 773 | `runtime turn_id does not carry its run_id` | `Runtime 的 turn_id 没有携带对应的 run_id` |
| 797 | `Agent turn timed out after {seconds} seconds` | `Agent Turn 在 {seconds} 秒后超时` |
| 814 | `unexpected Agent failure: {type}` | `未预期的 Agent 故障：{type}` |
| 1043 | `checkpoint belongs to another run` | `checkpoint 属于另一个运行` |
| 1045 | `checkpoint uses a different scenario` | `checkpoint 使用了不同的场景` |
| 1047 | `checkpoint uses a different seed` | `checkpoint 使用了不同的 seed` |
| 1049 | `checkpoint policy metadata differs from the active Agents` | `checkpoint 的策略元数据与当前 Agent 不同` |
| 1053 | `checkpoint must contain one cursor per company` | `checkpoint 必须为每家公司包含一个游标` |
| 1079 | `agents must match scenario companies; missing=..., unexpected=...` | `Agent 必须与场景公司一致；缺少=...，未预期=...` |
| 1093 | `an episode cannot mix replay and live Agents` | `一个 episode 不能混用 Replay Agent 和实时 Agent` |
| 1095 | `replay Agents require the completed source result` | `Replay Agent 需要已完成的源结果` |
| 1097 | `a replay source is valid only with replay Agents` | `Replay 源只能与 Replay Agent 一起使用` |
| 1099 | `replay source scenario and seed must match the runtime` | `Replay 源的场景和 seed 必须与 Runtime 匹配` |
| 1103 | `replay Agent journals must belong to the source run_id` | `Replay Agent Journal 必须属于源 run_id` |
