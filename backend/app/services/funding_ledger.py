"""修缮支出冲减台账。

变更签证的费用扣减必须落在「影响工序」这一条支出线上，不能借用其他工序
（例如西跨木料）的已核准金额。本模块集中维护：

* 某工序的已核准支出（只统计 ``status == 已核准`` 的支出，待核准款项不参与抵扣）；
* 该工序已经签认的减项冲减（含已签认待入账、已入账两种状态）；
* 可用余额 = 已核准支出 - 已签认冲减；
* 变更签证与修缮支出台账共用的冲减回执。

所有对余额有影响的写操作都由调用方在 ``funding_ledger`` 资源锁内完成，
保证减项签认和财务核准同一工序的新支出读到的是同一份余额。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.store import store

# 冲减状态：签认后先占用额度，财务登记实际退款后转为已入账；驳回未入账的签认会释放额度。
OFFSET_RESERVED = "已签认"
OFFSET_BOOKED = "已入账"
OFFSET_RELEASED = "已释放"

REFUND_CATEGORY = "减项退款"
REFUND_RECEIPT_FIELD = "冲减回执"
LEDGER_RESOURCE_KEY = "funding_ledger"


def as_amount(value: Any) -> float:
    """把金额字段转成 float；空值、非数字都按 0 处理。"""
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _round(value: float) -> float:
    return round(value + 0.0, 2)


def change_process(entry: dict[str, Any]) -> str:
    return str(entry.get("影响工序") or "").strip()


def funding_row_process(row: dict[str, Any]) -> str:
    return str(row.get("对应工序") or "").strip()


def is_refund_row(row: dict[str, Any]) -> bool:
    """是否为变更减项产生的退款/冲减行。

    以冲减回执或关联签证为主要标识，避免把普通「退款」类支出误当成
    变更冲减；同时兼容早期系统按支出类别登记的数据。
    """
    if str(row.get(REFUND_RECEIPT_FIELD) or "").strip():
        return True
    if str(row.get("关联签证") or "").strip():
        return True
    return str(row.get("支出类别") or "").strip() == REFUND_CATEGORY


def funding_row_belongs_to_process(row: dict[str, Any], process: str) -> bool:
    """判断支出行是否属于指定工序。

    优先使用显式的「对应工序」字段；为兼容既有登记习惯，未填该字段时
    才在支出项目和备注中查找工序名。绝不退回「全场支出汇总」。
    """
    process = str(process or "").strip()
    if not process:
        return False
    explicit = funding_row_process(row)
    if explicit:
        return explicit == process
    text = f"{row.get('支出项目', '')} {row.get('备注说明', '')}"
    return process in text


def approved_amount(process: str) -> float:
    """某工序已核准的支出合计；退款行不重复计入，未核准款项不参与。"""
    total = 0.0
    for row in store.rows("funding_use"):
        if is_refund_row(row):
            continue
        if str(row.get("status") or "") != "已核准":
            continue
        if not funding_row_belongs_to_process(row, process):
            continue
        amount = as_amount(row.get("实际金额"))
        if amount == 0:
            amount = as_amount(row.get("预算金额"))
        if amount > 0:
            total += amount
    return _round(total)


def active_offset_amount(process: str, exclude_change_id: Any = None) -> float:
    """某工序已经签认、仍占用可用余额的冲减合计。

    ``exclude_change_id`` 用于签认过程中排除当前这张尚未落账的签证，
    避免它把自己的冲减金额重复计入余额。
    """
    excluded = None
    if exclude_change_id is not None:
        try:
            excluded = int(exclude_change_id)
        except (TypeError, ValueError):
            excluded = exclude_change_id
    total = 0.0
    for entry in store.rows("change_order"):
        if change_process(entry) != str(process or "").strip():
            continue
        if excluded is not None and entry.get("id") == excluded:
            continue
        status = str(entry.get("冲减状态") or "")
        if status in (OFFSET_RESERVED, OFFSET_BOOKED):
            amount = as_amount(entry.get("已冲减金额"))
            if amount == 0:
                amount = abs(as_amount(entry.get("冲减金额")))
            total += amount
        elif not status and str(entry.get("status") or "") == "已签认":
            # 兼容没有冲减字段的历史签认数据：负费用视为已占用额度。
            fee = as_amount(entry.get("费用增减"))
            if fee < 0:
                total += abs(fee)
    return _round(total)


def available_balance(process: str, exclude_change_id: Any = None) -> float:
    """可用余额 = 本工序已核准支出 - 已经签认的冲减。"""
    return _round(approved_amount(process) - active_offset_amount(process, exclude_change_id))


def receipt_for_change(change_id: int | str) -> str:
    """一张签证对应一个稳定的冲减回执号。"""
    try:
        return f"OFFSET-{int(change_id):06d}"
    except (TypeError, ValueError):
        return f"OFFSET-{change_id}"


def find_change_order(change_id: Any) -> dict[str, Any] | None:
    try:
        entry_id = int(change_id)
    except (TypeError, ValueError):
        return None
    return store.find("change_order", entry_id)


def find_refund_row(change_id: Any) -> dict[str, Any] | None:
    """按关联签证查找该签证唯一的退款支出行。"""
    target = str(change_id)
    for row in store.rows("funding_use"):
        if not is_refund_row(row):
            continue
        if str(row.get("关联签证") or "") == target:
            return row
    # 兼容只登记了回执号、未写关联签证的财务登记方式。
    receipt = receipt_for_change(change_id)
    for row in store.rows("funding_use"):
        if is_refund_row(row) and str(row.get(REFUND_RECEIPT_FIELD) or "") == receipt:
            return row
    return None


def plan_offset(entry: dict[str, Any]) -> dict[str, Any] | None:
    """计算本次减项签认的额度方案；非减项返回 None。"""
    fee = as_amount(entry.get("费用增减"))
    if fee >= 0:
        return None
    process = change_process(entry)
    amount = _round(abs(fee))
    status = str(entry.get("冲减状态") or "")
    already_allocated = status in (OFFSET_RESERVED, OFFSET_BOOKED)
    available = available_balance(process, exclude_change_id=entry.get("id"))
    return {
        "process": process,
        "amount": amount,
        "available": available,
        "already_allocated": already_allocated,
        "sufficient": already_allocated or amount <= available + 1e-9,
        "shortfall": _round(max(0.0, amount - available)),
        "receipt": str(entry.get("冲减回执") or receipt_for_change(entry.get("id", ""))),
    }


def _next_funding_id() -> int:
    return max((int(row.get("id", 0)) for row in store.rows("funding_use")), default=0) + 1


def _refund_row_values(
    entry: dict[str, Any], process: str, amount: float, receipt: str
) -> dict[str, Any]:
    change_id = entry.get("id")
    return {
        "支出项目": f"{process}减项退款（签证#{change_id}）",
        "支出类别": REFUND_CATEGORY,
        "对应工序": process,
        "预算金额": -amount,
        "实际金额": -amount,
        "退款金额": amount,
        "冲减金额": amount,
        REFUND_RECEIPT_FIELD: receipt,
        "回执编号": receipt,
        "关联签证": change_id,
        "支出日期": entry.get("签认日期") or date.today().isoformat(),
        "经办人": entry.get("审批人") or "财务",
        "审批状态": "减项冲减待入账",
        "备注说明": f"冲减回执{receipt}；关联变更签证#{change_id}；减项{amount:.2f}元",
        "status": "已提交",
        "pending": True,
        "abnormal": False,
        "入账状态": "待入账",
    }


def commit_offset(entry: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any] | None:
    """把审核通过的减项落到签证回执和修缮支出台账上。

    同一张签证重复提交时复用已有退款行，不重复扣减。
    """
    process = plan["process"]
    amount = plan["amount"]
    receipt = plan["receipt"]
    change_id = entry.get("id")

    if str(entry.get("冲减状态") or "") not in (OFFSET_RESERVED, OFFSET_BOOKED):
        entry["冲减金额"] = amount
        entry["已冲减金额"] = amount
        entry["冲减状态"] = OFFSET_RESERVED
        entry["冲减回执"] = receipt
        entry["回执编号"] = receipt
        entry["冲减对应工序"] = process
        entry["签认前可用余额"] = plan["available"]
        entry["签认后可用余额"] = _round(plan["available"] - amount)
        entry["冲减时间"] = datetime.now().isoformat(timespec="seconds")

    row = find_refund_row(change_id)
    if row is None:
        row = {"id": _next_funding_id()}
        row.update(_refund_row_values(entry, process, amount, receipt))
        store.rows("funding_use").append(row)
    elif str(row.get("status") or "") != "已核准":
        # 已释放后重新签认：复用同一行，恢复为待入账，避免重复退款记录。
        row.update(_refund_row_values(entry, process, amount, receipt))

    entry["退款支出ID"] = row.get("id")
    return row


def release_offset(entry: dict[str, Any]) -> bool:
    """驳回签证：释放尚未入账的冲减额度，删除未入账退款行。

    已经入账的退款不能撤销，仍在修缮支出明细中保留原回执和凭据。
    """
    status = str(entry.get("冲减状态") or "")
    if status == OFFSET_RESERVED:
        entry["冲减状态"] = OFFSET_RELEASED
        entry["已冲减金额"] = 0
        entry["释放时间"] = datetime.now().isoformat(timespec="seconds")
        row = find_refund_row(entry.get("id"))
        if row is not None:
            entry["已释放退款支出ID"] = row.get("id")
            rows = store.rows("funding_use")
            if row in rows:
                rows.remove(row)
        entry.pop("退款支出ID", None)
        return True
    if status == OFFSET_BOOKED:
        # 已入账：额度已经变成实际退款，不能释放，也不删除支出凭据。
        entry["驳回时已入账"] = True
        row = find_refund_row(entry.get("id"))
        if row is not None:
            note = str(row.get("备注说明") or "")
            extra = "签证已驳回，退款已入账，保留原冲减回执作为凭据"
            if extra not in note:
                row["备注说明"] = f"{note}；{extra}" if note else extra
        return False
    return False


def book_offset(refund_row: dict[str, Any]) -> dict[str, Any] | None:
    """财务核准退款行：把这笔退款标记为已入账，并与签认回执对账。"""
    change_id = refund_row.get("关联签证")
    entry = find_change_order(change_id)
    if entry is None and refund_row.get(REFUND_RECEIPT_FIELD):
        receipt = str(refund_row.get(REFUND_RECEIPT_FIELD))
        for candidate in store.rows("change_order"):
            if str(candidate.get("冲减回执") or "") == receipt:
                entry = candidate
                break
    refund_row["入账状态"] = "已入账"
    refund_row["审批状态"] = "减项冲减已入账"
    refund_row["pending"] = False
    if entry is not None:
        amount = abs(as_amount(refund_row.get("退款金额")))
        if amount == 0:
            amount = abs(as_amount(refund_row.get("实际金额")))
        entry["冲减状态"] = OFFSET_BOOKED
        entry["已冲减金额"] = amount
        entry["冲减金额"] = amount
        entry["退款支出ID"] = refund_row.get("id")
        entry["入账时间"] = datetime.now().isoformat(timespec="seconds")
        entry["冲减对应工序"] = change_process(entry) or entry.get("冲减对应工序", "")
        entry[REFUND_RECEIPT_FIELD] = refund_row.get(REFUND_RECEIPT_FIELD) or receipt_for_change(entry.get("id", ""))
    return entry


def change_offsets(process: str) -> list[dict[str, Any]]:
    return [
        dict(entry) for entry in store.rows("change_order")
        if change_process(entry) == str(process or "").strip()
        and str(entry.get("冲减状态") or "") in (OFFSET_RESERVED, OFFSET_BOOKED, OFFSET_RELEASED)
    ]


def process_refunds(process: str) -> list[dict[str, Any]]:
    return [
        dict(row) for row in store.rows("funding_use")
        if is_refund_row(row) and funding_row_belongs_to_process(row, process)
    ]


def reconcile_process(process: str) -> dict[str, Any]:
    """给签认审批和修缮支出台账提供同一份对账结果。"""
    process = str(process or "").strip()
    approved = approved_amount(process)
    offset = active_offset_amount(process)
    return {
        "process": process,
        "approved_amount": approved,
        "signed_offset_amount": offset,
        "available_balance": _round(approved - offset),
        "change_orders": change_offsets(process),
        "refunds": process_refunds(process),
    }
