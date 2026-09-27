"""把内存实体序列化为确定性 JSON（枚举取 value，日期取 ISO）。"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date
from enum import Enum

from .models import Invoice, Subscription, Transaction, UsageRecord


def _jsonable(obj):
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, date):
        return obj.isoformat()
    if is_dataclass(obj):
        return {k: _jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def sub_dict(sub: Subscription) -> dict:
    d = _jsonable(sub)
    return d


def invoice_dict(inv: Invoice) -> dict:
    return _jsonable(inv)


def txn_dict(txn: Transaction) -> dict:
    return _jsonable(txn)


def usage_dict(rec: UsageRecord) -> dict:
    return _jsonable(rec)
