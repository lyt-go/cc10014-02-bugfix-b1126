"""修缮支出领域服务：支出审批与变更签证费用联动，超出预算后联动排期异常。

财务核准「减项退款」行时，凭签证的冲减回执把这笔冲减登记为已入账；
核准同工序的新支出时与签证签认共用一把台账锁，双方读到同一份余额版本。
"""
from __future__ import annotations

from typing import Any

from app.services import funding_ledger
from app.services.base import BaseModuleService
from app.store import store


class FundingUseService(BaseModuleService):
    MODULE = "funding_use"
    ENTITY = "支出记录"
    OPERATION_TERM = "提交报销"
    LIST_FIELDS = ["支出项目", "支出类别", "对应工序", "预算金额", "实际金额", "支出日期", "经办人", "审批状态", "备注说明"]
    REQUIRED_FIELDS = ["支出项目", "支出类别", "预算金额"]
    STATUS_ORDER = ["待提交", "已提交", "已核准", "已驳回"]
    ACTION_RULES = {"提交报销": "已提交", "核准支出": "已核准", "驳回支出": "已驳回"}
    NEGATIVE_ACTIONS = ["驳回支出"]

    def resource_keys(self, entry_id: int, action: str) -> list[str]:
        keys = super().resource_keys(entry_id, action)
        # 核准/驳回都会改变工序余额口径，与变更签证签认共用台账锁。
        keys.append(funding_ledger.LEDGER_RESOURCE_KEY)
        return keys

    def cross_module_verify(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> tuple[bool, str]:
        if action == "核准支出":
            actual = float(entry.get("实际金额", 0))
            budget = float(entry.get("预算金额", 0))
            if funding_ledger.is_refund_row(entry):
                # 退款行必须能对上唯一一张已签认的减项签证，避免重复入账。
                change = funding_ledger.find_change_order(entry.get("关联签证"))
                receipt = str(entry.get(funding_ledger.REFUND_RECEIPT_FIELD) or "").strip()
                if change is None and receipt:
                    for candidate in store.rows("change_order"):
                        if str(candidate.get("冲减回执") or "") == receipt:
                            change = candidate
                            break
                if change is None:
                    return False, "减项退款缺少对应签证，无法核准入账"
                if not str(entry.get("关联签证") or "").strip():
                    entry["关联签证"] = change.get("id")
                    entry[funding_ledger.REFUND_RECEIPT_FIELD] = (
                        receipt or funding_ledger.receipt_for_change(change.get("id", ""))
                    )
                if str(change.get("冲减状态") or "") not in (
                    funding_ledger.OFFSET_RESERVED, funding_ledger.OFFSET_BOOKED
                ):
                    return False, "签证冲减未签认或已释放，不能登记退款"
                if str(entry.get("入账状态") or "") == "已入账" or entry.get("status") == "已核准":
                    return False, "该笔减项退款已入账，不能重复登记"
                return True, ""
            # 超额检查
            if actual > budget * 1.1:
                # 检查是否有变更签证支撑超额
                cs = [
                    r for r in store.rows("change_order")
                    if r.get("status") == "已签认" and "费用增减" in str(r.keys())
                ]
                has_support = any(
                    funding_ledger.as_amount(c.get("费用增减")) >= (actual - budget)
                    for c in cs
                )
                if not has_support:
                    return False, f"超出预算{actual - budget:.2f}元，需变更签证支撑"
        return True, ""

    def cross_module_effect(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> list[dict]:
        effects = []
        if action == "核准支出":
            if funding_ledger.is_refund_row(entry):
                # 财务凭冲减回执登记本工序实际退款，回写签证冲减状态形成对账闭环。
                change = funding_ledger.book_offset(entry)
                if change is not None:
                    effects.append(dict(change))
                return effects

            # 核准后更新竣工总结中的费用汇总
            actual = float(entry.get("实际金额", 0))
            for summary in store.rows("completion_summary"):
                if summary.get("status") in ("编制中", "已提交"):
                    summary["费用已核准"] = True
                    effects.append(dict(summary))
        return effects
