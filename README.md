# Subscription Billing & Metering（本地可跑）

一套单进程 FastAPI 订阅计费与用量计量服务。全部依赖本地化：内存存储、
确定性时钟、模拟支付网关，**不依赖外部服务或真实凭据**。重点保证计费结果在
**重复请求、乱序事件、并发扣款**下稳定且可解释。

- Python 3.14 / FastAPI / pytest
- 金额单位统一为"分"（整数），避免浮点误差
- 时间统一 UTC；金额一旦出账即冻结

---

## 1. 快速开始

```bash
uv sync                       # 安装依赖（含 pytest）
uv run uvicorn main:app --reload --port 8000
# 交互式接口文档：http://127.0.0.1:8000/docs
```

跑测试（确定性、无网络）：

```bash
uv run pytest -q
```

---

## 2. 核心规则

### 2.1 账期（Period）

- 每个订阅有 `anchor_day`（1–28），账期为月度锚点界定的左闭右开区间
  `[period_start, period_end)`。
- 首个账期从订阅创建时刻开始，到下一个锚点日 00:00 UTC。
- 用量按**业务发生时间 `occurred_at`** 归属账期，而不是上报顺序，因此
  延迟/乱序上报都能落回正确账期。

### 2.2 按比例计费（Proration）

同一账期内的**创建 / 升级 / 降级 / 立即取消**都会产生一条 `PlanChange`
分段，按自然日切分：

```
分段金额 = floor(套餐基础价(分) × 分段天数 / 账期天数)
```

- 每次变更都对**当时的套餐价格做快照**（`plan_code` + `plan_version`）。
- 每个账期多次变更，按 `effective_at` 排序切分，结果**确定且可解释**：
  账单每行 `detail` 中都带 `formula`（如 `floor(3000 * 18 / 28)`）、
  `segment_days`、`period_days`、单价等判定依据。
- 向下取整到最小货币单位；各分段之和即基础费，永不回算。

**取消**：

- `immediate`：当前账期按天结到取消时刻，之后不再计费，订阅立即 `canceled`。
- `end_of_period`：当前账期按整期正常出账，账期推进后状态收敛为
  `canceled`，不再开新账期。

**历史不可变（硬约束）**：

- `finalize` 是冻结动作：金额、明细、价格快照落定后，重复出账**原样返回**。
- 后续套餐调价只新增版本，**不覆盖** `(code, version)` 历史快照，已出账/
  已支付账单完全不受影响。

### 2.3 用量计量与超额（Metering & Overage）

- 账期内各分段"按比例包含额度" = `floor(套餐包含额度 × 分段天数 / 账期天数)`，
  上一段未用完的额度可向后续段结转。
- 超过可用额度的单位按**事件所在分段的超额单价**计费，在账单中以独立的
  `overage` 行体现（与基础费 `proration`、包含额度 `included` 分开列示）。
- 幂等与去重：
  - `event_id` 是业务事件唯一键：相同 `event_id` 重复上报**只计一次**。
  - `idempotency_key` 是请求键：同键不同事件载荷返回 `409 duplicate_event`。
  - 归属已关账账期的事件：**先持久化**再返回 `409 period_closed`，事件不丢；
    通过**账后调整单**入账，历史主账单金额保持不变。

调整单（adjustment bill）：

- `POST /bills/{bill_id}/adjustments` 汇总关账后到达的迟到/乱序用量，
  生成 `is_adjustment=true` 的独立账单，单价取主账单最后有效分段的超额价；
  没有新事件时返回 `{"created": false}`，天然幂等。

### 2.4 扣款、失败原因、重试与宽限

模拟支付网关根据"卡片是否有效 + 账户余额"判定：

| 结果 | 含义 | 是否可重试 | 账单 failure_reason |
|---|---|---|---|
| `success` | 扣款成功并扣减余额 | — | `none` |
| `insufficient_funds` | 余额不足 | 是 | `insufficient_funds` |
| `gateway_error` | 网关错误/无支付方式 | 是 | `gateway_error` / `no_payment_method` |
| `card_declined` | 卡片失效（终态） | 否 | `card_declined` |
| 重试次数用尽 | — | 否 | `retry_exhausted` |

- **退避节奏**：第 n 次失败后，下一次重试在
  `base_interval × 2^(n-1)` 秒后（默认 3600s，指数退避，参数可调）。
- **扣款幂等键**：`charge:{bill_id}:{attempt_no}`。"检查—网关扣款—写流水—
  账单状态迁移"全部在同一把存储锁内完成，因此：
  - 同一账单的**并发扣款最多真实成功一次**，余额只扣一次；
  - 重试成功后再扣款不会重复收钱（返回既有成功尝试）；
  - 已付清账单的重复/并发扣款复用同一成功尝试。
- **资金流水**：每次尝试一条流水，成功记正收入，失败记 0 元失败流水
  （保留审计轨迹）。`GET /bills/{id}/reconcile` 校验
  "成功流水之和 == 账单应付金额"。
- **宽限期**：自首次失败起算，默认 3 天。
  - 期内付清 → 订阅恢复 `active`；
  - 期内未付清 → 订阅 `past_due`（仍可重试）；
  - 宽限结束仍未付清 → 订阅 `expired`，未结账单置 `failed(retry_exhausted)`。

订阅状态机：`active → past_due → active`（补缴）或 `past_due → expired`；
取消路径为 `active → canceled`。

### 2.5 分次付款、退款与账户抵扣

**分次付款**（`POST /bills/{id}/payments`）：

- 已出账（`finalized`）账单可以一笔一笔付，直到付清；账单上的
  `paid_cents` / `remaining_cents` 实时反映"已收 / 还差多少"。
- 每笔付款由调用方提供 `idempotency_key`：重复提交返回首次结果，
  **不重复扣钱**。
- 并发安全：判定与扣款在同一锁内，同一张账单的并发付款**合计实收不超
  过应付**；超出剩余应付的付款被拒绝（`409 overpayment`），付满后账单
  唯一收敛为 `paid`，**后来的请求按已结清处理**（`already_settled=true`），
  不再收钱。
- 与整笔扣款兼容：自动扣款 / 重试按**剩余应付**（应付 − 已实收 − 已抵扣）
  收取，先分次付一部分再触发扣款不会重复收钱；两条路径产生的实收都登记
  为 `Payment`，作为后续退款的锚点。

**退款**（`POST /payments/{id}/refunds`）：

- 已收到的钱可整笔退也可退一部分；累计退款**不得超过该笔实收**
  （`payment.refundable_cents`），超额拒绝（`409 refund_exceeded`）。
- 退款**不改写历史账单**：账单金额、明细、状态保持原样；资金变化只体现
  在流水（`refund` 负金额）与账户抵扣额度上，冻结账目不重算。
- 退款记录锚定到具体付款（`payment_id`），能说清退的是哪一笔；
  `idempotency_key` 重复提交返回首次退款，不重复退钱。
- 退回的钱默认沉淀为**账户抵扣额度**（`credit_balance_cents`）。

**账户抵扣**（出账时自动应用）：

- 之后任何账单出账（含调整单）时，自动用抵扣额度冲抵：
  `抵扣额 = min(账户可用额度, 账单剩余应付)`；额度够则账单直接 `paid`，
  不够的部分仍走原支付方式（分次付款 / 自动扣款）。
- 一张账单**只应用一次**（`credit_app_by_bill` 仲裁），不会重复抵扣；
  每次应用都有独立的 `CreditApplication` 记录与 `credit_apply` 流水，
  可按账核对。
- 账户不变量：`credit_balance = 累计退款 − 累计抵扣`，不会抵成负数；
  多张账单按出账顺序依次冲抵。

**对账口径**（`GET /bills/{id}/reconcile`）：

- `paid_cents` = 实收毛额（自动扣款成功 + 分次付款），`credit_applied_cents`
  = 抵扣，`refunded_cents` = 已退，`net_received_cents` = 实收 − 已退。
- `balanced`：已付清账单要求 实收 + 抵扣 == 应付 且 已退 ≤ 实收；
  未付清账单要求 实收 + 抵扣 ≤ 应付。

---

## 3. 幂等与并发约定（汇总）

| 操作 | 幂等键 | 重复提交行为 |
|---|---|---|
| 创建订阅/升降级/取消 | 请求体 `idempotency_key` | 返回首次结果，不新增分段 |
| 用量上报 | `event_id`（+ 请求键 `idempotency_key`） | 同事件只计一次；同键异载荷 409 |
| 出账 | `bill:{sub}:{period}` | 已冻结账单原样返回，不重算 |
| 扣款 | `charge:{bill}:{attempt_no}` | 同尝试不重复扣款；并发仅一次成功 |
| 分次付款 | 请求体 `idempotency_key` | 重复提交返回首次付款，不重复扣钱 |
| 退款 | 请求体 `idempotency_key` | 重复提交返回首次退款，不重复退钱 |
| 抵扣 | `credit:{bill_id}`（每单限一次） | 重复触发直接跳过，不重复抵扣 |
| 调整单 | 按"未计费迟到事件"动态汇总 | 无新事件返回 `created=false` |

并发安全：进程内单把 `threading.RLock` 串行化所有跨实体写操作；测试用
线程池（12 路屏障）验证并发扣款与并发重复上报。

---

## 4. HTTP 接口

### 管理/测试设施
- `POST /plans` 创建/调价（自动递增 version，旧版本保留）
- `GET /plans/{code}?version=` 查询价格（含历史版本）
- `POST /accounts` 设置模拟账户（余额、卡片是否有效）
- `POST /admin/clock`、`POST /admin/clock/advance?seconds=&days=` 冻结/推进时间
- `GET /health`

### 订阅
- `POST /subscriptions` 创建（幂等）
- `GET /subscriptions/{id}` 详情（含当前账期变更序列）
- `POST /subscriptions/{id}/change` 升级/降级（幂等）
- `POST /subscriptions/{id}/cancel` 取消（immediate / end_of_period）

### 用量与账单
- `POST /subscriptions/{id}/usage` 上报用量（重复/乱序安全）
- `GET /subscriptions/{id}/bills` 列出主账单
- `POST /subscriptions/{id}/bills/finalize` 出账（冻结）
- `POST /subscriptions/{id}/advance` 推进账期
- `GET /bills/{id}`、`POST /bills/{id}/adjustments`
- `POST /bills/{id}/charge` 立即扣款（按剩余应付收取）
- `POST /bills/{id}/retries?max_attempts=&base_interval_sec=&grace_seconds=`
  处理到期重试并应用宽限策略
- `GET /bills/{id}/reconcile` 对账

### 分次付款 / 退款 / 抵扣
- `POST /bills/{id}/payments` 分次付款（`amount_cents` + `idempotency_key`）
- `GET /bills/{id}/payments` 列出账单实收付款（含可退余额）
- `POST /payments/{id}/refunds` 退款（缺省退该笔全部可退余额）
- `GET /bills/{id}/refunds` 列出账单相关退款
- `GET /bills/{id}/credits` 列出账单的抵扣记录
- `GET /accounts/{customer_id}` 查询余额与抵扣额度

---

## 5. 本地验证脚本（curl 示例）

```bash
# 1) 账户与套餐
curl -s -XPOST localhost:8000/accounts -H 'Content-Type: application/json' \
  -d '{"customer_id":"c1","balance_cents":50,"card_active":true}'
curl -s -XPOST localhost:8000/plans -H 'Content-Type: application/json' \
  -d '{"code":"pro","name":"Pro","base_price_cents":3000,"included_units":500,"overage_unit_price_cents":2}'

# 2) 创建订阅（固定时间，便于复算）
curl -s -XPOST localhost:8000/admin/clock -H 'Content-Type: application/json' \
  -d '{"now":"2026-02-01T09:00:00+00:00"}'
curl -s -XPOST localhost:8000/subscriptions -H 'Content-Type: application/json' \
  -d '{"customer_id":"c1","plan_code":"basic","anchor_day":1,"idempotency_key":"s1"}'

# 3) 重复上报同一用量（只计一次）
for i in 1 2 3; do
curl -s -XPOST localhost:8000/subscriptions/$SID/usage -H 'Content-Type: application/json' \
  -d '{"event_id":"e1","quantity":130,"occurred_at":"2026-02-03T09:00:00+00:00","idempotency_key":"u1"}'
done

# 4) 出账 -> 余额不足失败 -> 充值 -> 过退避点重试成功 -> 对账
curl -s -XPOST localhost:8000/subscriptions/$SID/bills/finalize
curl -s -XPOST localhost:8000/bills/$BILL/charge
curl -s -XPOST localhost:8000/accounts -H 'Content-Type: application/json' \
  -d '{"customer_id":"c1","balance_cents":100000,"card_active":true}'
curl -s -XPOST "localhost:8000/admin/clock/advance?seconds=3601"
curl -s -XPOST "localhost:8000/bills/$BILL/retries?base_interval_sec=3600"
curl -s localhost:8000/bills/$BILL/reconcile

# 5) 分次付款：分两笔付清一张新账单，重复提交第二笔不重复扣钱
curl -s -XPOST localhost:8000/subscriptions -H 'Content-Type: application/json' \
  -d '{"customer_id":"c1","plan_code":"basic","anchor_day":1,"idempotency_key":"s2"}'
curl -s -XPOST localhost:8000/subscriptions/$SID2/bills/finalize
curl -s -XPOST localhost:8000/bills/$BILL2/payments -H 'Content-Type: application/json' \
  -d '{"amount_cents":400,"idempotency_key":"p1"}'     # remaining_cents 可见
curl -s -XPOST localhost:8000/bills/$BILL2/payments -H 'Content-Type: application/json' \
  -d '{"amount_cents":600,"idempotency_key":"p2"}'     # -> paid
curl -s -XPOST localhost:8000/bills/$BILL2/payments -H 'Content-Type: application/json' \
  -d '{"amount_cents":600,"idempotency_key":"p2"}'     # duplicate=true，不再扣

# 6) 退款：退第一笔的一部分，钱进入账户抵扣额度
curl -s localhost:8000/bills/$BILL2/payments           # 找到 p1 的 payment id
curl -s -XPOST localhost:8000/payments/$PAY1/refunds -H 'Content-Type: application/json' \
  -d '{"amount_cents":150,"reason":"部分不满意","idempotency_key":"rf1"}'
curl -s localhost:8000/accounts/c1                     # credit_balance_cents = 150

# 7) 抵扣：下一张账单出账时自动用额度冲抵，剩余再走付款
curl -s -XPOST localhost:8000/subscriptions -H 'Content-Type: application/json' \
  -d '{"customer_id":"c1","plan_code":"basic","anchor_day":1,"idempotency_key":"s3"}'
curl -s -XPOST localhost:8000/subscriptions/$SID3/bills/finalize
curl -s localhost:8000/bills/$BILL3                    # credit_applied_cents=150
curl -s localhost:8000/bills/$BILL3/reconcile          # balanced=true
```

---

## 6. 测试覆盖

`uv run pytest -q` 覆盖以下关键场景：

- **重复上报**：同 `event_id` 连报 5 次 / 10 线程并发同报，只计一次。
- **乱序/延迟上报**：按发生时间乱序写入，账期累计与顺序无关。
- **超额计费**：超额行独立、金额确定，重复出账不重算。
- **按比例费用**：账期内多次升级/降级分段金额固定；立即取消按天结。
- **历史不可变**：取消后、套餐调价后，已出账/已付账单金额与明细不变。
- **账后迟到用量**：`409 period_closed` + 独立调整单，不漏不重、主单不动。
- **并发扣款**：12 路并发仅一次真实扣款、一条成功流水、余额只扣一次。
- **失败原因区分**：余额不足 / 卡片失效（终态）/ 无支付方式。
- **重试耗尽**：指数退避到点才重试，次数用尽置 `failed(retry_exhausted)`。
- **宽限收敛**：期内补缴恢复 active；超时订阅 expired。
- **对账**：失败流水为 0、成功流水唯一且等于账单金额。
- **分次付款**：逐笔付满后收敛 `paid`；同键重复提交不重复扣钱；10 路并发
  付款合计不超收；超付拦截；已结清后请求不再收钱；剩余应付实时可见。
- **退款**：整笔/部分退，累计不超实收；退款不改写历史账单金额与状态；
  幂等防重；退回金额进入账户抵扣额度。
- **账户抵扣**：出账自动冲抵，额度够则直接 `paid`；不足部分走原支付方式；
  按出账顺序生效、每单只抵一次；账户不变量 退款−抵扣=额度 恒成立。
- **端到端**：HTTP 全链路 分次付款→退款→额度→下一账单冲抵→对账。

日志默认在 INFO 级别打印关键输入与判定依据（幂等键、账期窗口、分段天数、
公式、网关判定、失败原因、退避时间点、对账结果）。

## 7. 代码结构

```
app/
  models.py         枚举与领域数据结构（含套餐价格快照）
  clock.py          可冻结/推进的确定性时钟
  period.py         账期边界、事件日期归属、分段天数（纯函数）
  pricing.py        按比例基础费 / 包含额度 / 超额（纯函数，带公式明细）
  store.py          内存存储 + 全局锁 + 版本快照与幂等索引
  subscriptions.py  创建/升级/降级/取消
  metering.py       用量幂等上报与账期归集
  billing.py        出账冻结、调整单、账期推进（出账时触发抵扣）
  payments.py       模拟网关、幂等扣款、分次付款、失败原因、退避计算
  refunds.py        退款（锚定付款、不超实收、退回抵扣额度）
  credits.py        账户抵扣额度（出账自动冲抵、每单一次、可对账）
  grace.py          重试调度、重试耗尽、宽限期收敛
  ledger.py         资金流水与对账
  api.py            FastAPI 路由
  schemas.py / serializers.py
tests/              pytest 场景测试（TestClient + 线程池并发）
```
