"""变更签证领域服务：签认签证后直接更新施工排期里程碑和费用联动。

减项签证（费用增减为负）只能在「影响工序」自身已经核准的支出余额内冲减，
不得借用其他工序（如西跨木料）的已核准金额。冲减结果同步写入修缮支出台账，
由财务凭冲减回执登记本工序的实际退款。
"""
from __future__ import annotations

from typing import Any

from app.services import funding_ledger
from app.services.base import BaseModuleService
from app.store import store


class ChangeOrderService(BaseModuleService):
    MODULE = "change_order"
    ENTITY = "变更签证单"
    OPERATION_TERM = "提出变更"
    LIST_FIELDS = ["变更事项", "变更原因", "影响工序", "费用增减", "提出人", "审批人", "签认日期", "备注说明"]
    REQUIRED_FIELDS = ["变更事项", "变更原因", "影响工序"]
    STATUS_ORDER = ["待提出", "已提出", "已签认", "已驳回"]
    ACTION_RULES = {"提出变更": "已提出", "签认变更": "已签认", "驳回变更": "已驳回"}
    NEGATIVE_ACTIONS = ["驳回变更"]

    def resource_keys(self, entry_id: int, action: str) -> list[str]:
        keys = super().resource_keys(entry_id, action)
        # 签认/驳回会读写共享的工序余额，必须与修缮支出的核准动作共用一把台账锁。
        keys.append(funding_ledger.LEDGER_RESOURCE_KEY)
        return keys

    def cross_module_verify(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> tuple[bool, str]:
        if action == "签认变更":
            process = entry.get("影响工序", "")
            # 1. 校验被影响的工序/排期确实存在
            schedules = store.query_by_field("construction_schedule", "计划工序", process)
            if not schedules:
                return False, f"受影响工序「{process}」在施工排期中不存在"
            # 2. 不能对已完成验收的工序签认变更
            accs = store.query_by_field("acceptance_record", "验收工序", process)
            if any(a.get("status") == "已签署" for a in accs):
                return False, f"工序「{process}」已有签署验收，不能签认变更"
            # 3. 减项只能在对应工序自身已核准余额内冲减
            plan = funding_ledger.plan_offset(entry)
            if plan is not None and not plan["sufficient"]:
                if plan["available"] <= 0:
                    return False, (
                        f"工序「{process}」没有可冲减的已核准支出，"
                        f"减项{plan['amount']:.2f}元无法签认，尚缺{plan['shortfall']:.2f}元；"
                        f"请待该工序支出核准后再提交"
                    )
                return False, (
                    f"工序「{process}」可冲减余额仅{plan['available']:.2f}元，"
                    f"减项{plan['amount']:.2f}元尚缺{plan['shortfall']:.2f}元，"
                    f"请调整减项金额或待该工序支出核准后再提交"
                )
        return True, ""

    def cross_module_effect(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> list[dict]:
        effects = []
        if action == "签认变更":
            process = entry.get("影响工序", "")
            # 1. 更新施工排期里程碑节点
            for sched in store.rows("construction_schedule"):
                if sched.get("计划工序", "") == process:
                    sched["里程碑节点"] = f"变更签证#{entry_id}：{entry.get('变更事项', '')}"
                    sched["abnormal"] = True
                    effects.append(dict(sched))

            fee_change = funding_ledger.as_amount(entry.get("费用增减"))
            if fee_change < 0:
                # 2. 减项：占用本工序已核准余额，并同步生成唯一的一笔退款支出。
                plan = funding_ledger.plan_offset(entry)
                if plan is None:
                    plan = funding_ledger.plan_offset({**entry, "费用增减": -abs(fee_change)})
                refund_row = funding_ledger.commit_offset(entry, plan)
                if refund_row is not None:
                    effects.append(dict(refund_row))
            elif fee_change > 0:
                # 3. 增项：创建修缮支出追加记录，补齐对应工序便于台账核对。
                funding_rows = store.rows("funding_use")
                new_funding = {
                    "id": max((int(r.get("id", 0)) for r in funding_rows), default=0) + 1,
                    "支出项目": f"变更签证#{entry_id}费用调整",
                    "支出类别": "变更追加",
                    "对应工序": process,
                    "预算金额": fee_change,
                    "实际金额": fee_change,
                    "支出日期": entry.get("签认日期", ""),
                    "经办人": entry.get("审批人", ""),
                    "审批状态": "变更费用",
                    "备注说明": f"由变更签证#{entry_id}自动触发",
                    "关联签证": entry_id,
                    "status": "待提交",
                    "pending": True,
                    "abnormal": False,
                }
                funding_rows.append(new_funding)
                effects.append(new_funding)

            # 4. 更新关联工序异常标记
            for ws_row in store.rows("work_sequence"):
                if ws_row.get("工序名称", "") == process:
                    ws_row["abnormal"] = True
                    effects.append(dict(ws_row))

        elif action == "驳回变更":
            # 驳回后释放尚未入账的冲减额度；已入账退款保留凭据。
            funding_ledger.release_offset(entry)
        return effects
