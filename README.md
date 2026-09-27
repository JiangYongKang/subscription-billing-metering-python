# 订阅计费与用量计量服务（本地可运行）

一套基于 FastAPI 的**纯本地**订阅计费 / 按量计量 / 模拟扣款服务，重点解决：
重复请求、乱序事件、并发扣款下计费结果的**稳定性与可解释性**。
无外部服务、无真实支付凭据，支付网关为进程内确定性模拟。

## 1. 运行与本地验证

```bash
uv sync --group dev          # 安装 fastapi + pytest（全部为本地依赖）
uv run pytest                # 24 个用例，含并发/乱序/重试耗尽场景
uv run uvicorn main:app --reload   # 启动服务，打开 /docs 可交互调试
```

所有会改变时间的接口都支持 `?as_of=YYYY-MM-DD` 查询参数，便于确定性地
快进到任意日期验证出账、重试与宽限，无需真实等待。

快速体验（另开终端）：

```bash
curl -s -X POST localhost:8000/subscriptions \
  -H 'content-type: application/json' \
  -d '{"tier":"basic","start_date":"2026-09-01"}'
curl -s "localhost:8000/billing/run" -H 'content-type: application/json' \
  -d '{"as_of":"2026-10-01"}'
curl -s localhost:8000/reconcile
```

## 2. 数据模型与金额约定

- 金额一律以**整数分**存储/计算，杜绝浮点误差。
- 账期 = **自然月** `[每月1日, 下月1日)`。
- 套餐（`app/catalog.py`）：

  | 套餐 | 月费 | 包含额度 | 超额单价 |
  |---|---|---|---|
  | basic | 1000 分（10 元） | 100 单位 | 20 分/单位 |
  | pro | 3000 分 | 500 单位 | 10 分/单位 |
  | enterprise | 10000 分 | 2000 单位 | 5 分/单位 |

## 3. 账单生命周期

```
DRAFT（账期进行中，金额可随变更/用量重算）
  ──账期结束或立即取消时出账（issued_on 固化、finalized=true）──▶ OPEN
        ├─ 扣款成功 ──────────────▶ PAID   （金额、行项永久冻结）
        ├─ 卡片失效等永久失败 ────▶ FAILED （原因 card_declined）
        └─ 余额不足等可重试失败，按节奏重试直至：
               · 成功 ▶ PAID（订阅由 past_due 恢复 active）
               · 次数耗尽 ▶ FAILED（原因 retry_exhausted）
```

已出账账单（OPEN/PAID/FAILED）金额与明细**不可再变**；只有 DRAFT 允许重算。

## 4. 按比例计费规则（升级 / 降级 / 多次变更）

- 账期内每次套餐变更都把时间轴切成**互不重叠、首尾相接**的片段，
  **每个自然日按当天有效套餐恰好计费一次**。因此不存在“先整收再退款”
  的负向冲抵行，升级降级用同一套公式，不会对同一天重复计费：

  `片段费用 = 套餐月费 × 片段天数 ÷ 账期天数`

- 片段费用通常不足整分。算法先对每行向下取整，再用**最大余数法**
  （余数相同则靠前片段优先，顺序确定）把不足 1 分的差额确定性地分到
  具体行，保证：**各行金额之和严格等于账单总额**，且任何人都能用相同
  输入复算出完全一致的结果。
- 同一天多次变更会合并为“当天直接选最终套餐”，与提交几次中间态无关。
- 变更必须按时间顺序提交（生效日不得早于上一次变更日）；乱序回填会被
  拒绝（错误码 `change_order_violation`），避免事后篡改账期构成。
- 每行明细都带 `meta`：`unit_price_cents / segment_days / period_days`，
  账单本身就是可解释的计费依据。
- **取消**：
  - 账期末取消（`at_period_end=true`）：当前账期正常出全月费用，账期
    结束后停止，不续费。
  - 立即取消（`at_period_end=false`）：费用只计到取消日当天，出账后
    立即冻结。取消后历史账期不会被任何后续操作重算。
- **调价**：`POST /admin/plans/{tier}/price` 只影响仍在 DRAFT 的费用与
  新账期；已出账账单的单价在出账时已固化（行 meta 可查），调价不改写历史。

## 5. 按量计量、幂等与超额

- 用量事件以 **(订阅, event_id)** 唯一去重：
  - 重复上报（哪怕数量/日期不同）直接返回首次记录、不二次累计；
  - 乱序、延迟上报在同一账期内等价，账期累计只取决于事件集合；
  - 事件按 **occurred_on（事件发生日）** 归属账期，而非上报时间。
- 超额：每段套餐的包含额度按该片段天数比例折算；落在片段内的用量超出
  额度的部分，按该套餐超额单价**单独生成 `overage` 行**，与基础费分开列示。
- 账期出账后收到的晚到事件会被拒绝（`period_closed`），不允许补记进
  已冻结的历史账目（如需补记应走下一账期的调整流程）。

## 6. 扣款幂等与并发约定

- 每次扣款携带幂等键 `idempotency_key`（默认 `inv:{账单}:attempt:{序号}`）；
  同键的并发/重复请求只产生**一条**流水，重复调用返回原流水。
- 所有状态变更在单把存储锁内串行化。账单一旦 PAID，后续任何扣款请求
  （含并发）都幂等返回原成功流水 —— **同一账单最多成功扣款一次**。
- 周期任务 `POST /billing/run` 也可多线程/重复触发：账期最多被结转一次，
  账单、扣款、续费都不会重复（见 `tests/test_concurrency.py`）。

## 7. 失败原因、重试节奏与宽限策略

失败原因（`FailureReason`，测试与日志据此区分）：

| 原因 | 含义 | 是否继续重试 |
|---|---|---|
| `insufficient_funds` | 余额不足 | 是（可配置节奏） |
| `card_declined` | 卡片失效 | 否，立即终止 |
| `retry_exhausted` | 重试次数耗尽 | 否 |
| `unknown` | 未知网关异常 | 是 |

- 默认节奏（`RetryConfig`，可整体替换）：出账当天首扣，失败后
  **第 1、4、11 天**各重试一次，共 4 次尝试；未到计划重试日时周期任务
  不提前扣款。
- 订阅在账单未付清期间为 `past_due`；宽限期默认自出账日起 **14 天**，
  宽限期内补交成功即恢复 `active`。**宽限期结束仍未付清（含重试耗尽或
  卡片失效）订阅收敛为 `canceled`**，欠款账单继续保留。边界：宽限截止日
  当天仍保留服务，次日才取消。
- 模拟网关由订阅创建参数控制，便于构造确定性场景：
  `payment_scenario=ok|insufficient_funds|card_declined`，
  并可用 `fail_times=N` 指定前 N 次余额不足、之后成功（模拟充值后补扣）。

## 8. 对账

`GET /reconcile` 校验资金流水与账单账目严格一致：

1. 每张 PAID 账单**恰好一条**成功流水且金额相等；
2. 成功流水总额 == 所有 PAID 账单总额；
3. 流水笔数与账单 `attempts` 记录一致，失败流水不产生资金入账；
4. 不存在无主流水、非 PAID 账单不允许有成功流水。

## 9. 主要接口

| 方法 & 路径 | 说明 |
|---|---|
| `POST /subscriptions` | 开通订阅（可指定套餐、起始日、支付场景） |
| `POST /subscriptions/{id}/change` | 升/降级（按比例分段，生效日默认当天） |
| `POST /subscriptions/{id}/cancel` | 取消（立即 / 账期末） |
| `GET  /subscriptions/{id}` | 订阅状态（active/past_due/canceled） |
| `POST /subscriptions/{id}/usage` | 上报用量（event_id 幂等） |
| `GET  /subscriptions/{id}/invoices` | 账单列表（含按比例行、超额行、尝试次数、失败原因） |
| `POST /invoices/{id}/charge` | 手动扣款（可带 idempotency_key） |
| `POST /billing/run` | 推进出账/重试/宽限收敛/续费（可带 as_of） |
| `GET  /reconcile` | 资金与账目对账 |
| `POST /admin/plans/{tier}/price` | 套餐调价（不改历史） |

## 10. 代码结构

```
app/models.py     枚举/实体/API schema（金额整数分）
app/config.py     重试节奏与宽限期配置
app/catalog.py    套餐目录与调价
app/pricing.py    账期、确定性按比例与超额计算
app/metering.py   用量幂等上报与账期累计
app/payments.py   模拟网关、扣款幂等、重试/耗尽判定
app/billing.py    生命周期编排、周期任务、宽限收敛、对账
app/store.py      带锁内存存储（线程安全）
app/api.py        FastAPI 路由
tests/            按比例/计量/支付并发/重试耗尽/生命周期/并发周期 用例
```

关键输入与判定依据均通过日志输出（如 `usage-duplicate`、
`charge-failed ... reason=... -> 计划/重试耗尽`、`grace-expired`、
`reconcile-ok/failed`），排查时可直接看到每次决策的原因。
