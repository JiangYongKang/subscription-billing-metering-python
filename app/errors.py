"""业务错误类型。"""


class BillingError(Exception):
    """所有业务错误的基类。"""

    code = "billing_error"
    http_status = 400


class NotFoundError(BillingError):
    code = "not_found"
    http_status = 404


class ConflictError(BillingError):
    code = "conflict"
    http_status = 409


class StateError(BillingError):
    """订阅/账单状态不允许该操作。"""

    code = "invalid_state"
    http_status = 409


class DuplicateEventError(ConflictError):
    """幂等冲突：相同 event_id / idempotency_key 已存在但载荷不同。"""

    code = "duplicate_event"


class PeriodClosedError(ConflictError):
    """用量事件归属账期已关账，需要走账后调整流程。"""

    code = "period_closed"


class PaymentTerminalError(BillingError):
    """扣款终态失败（卡片失效 / 重试耗尽）。"""

    code = "payment_terminal"
    http_status = 402


class PaymentDeclinedError(BillingError):
    """分次付款被网关拒绝（余额不足 / 卡片失效），未产生任何资金变动。"""

    code = "payment_declined"
    http_status = 402


class OverpaymentError(BillingError):
    """付款金额超过账单剩余应付。"""

    code = "overpayment"
    http_status = 409


class RefundExceededError(BillingError):
    """退款金额超过该笔付款的实际可退余额。"""

    code = "refund_exceeded"
    http_status = 409
